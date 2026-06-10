from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Callable

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient
from auto_research.models import AppConfig, PlanPolishRequest, PlanRequest
from auto_research.paths import ROOT
from auto_research.storage import read_json, run_dir, sync_latest, update_manifest, write_json, write_text


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _fallback_initial_plan(idea: dict) -> dict:
    initial_experiment = _idea_initial_experiment(idea)
    new_method = _idea_new_method(idea)
    base_steps = []
    if initial_experiment:
        base_steps.append(f"Use this initial experiment as the execution contract: {initial_experiment}")
    if new_method:
        base_steps.append(f"Implement the smallest testable change for the proposed method: {new_method}")
    base_steps.extend([
        "Verify repo/data/protocol gates before running commands; keep the plan blocked if the environment evidence is missing.",
        "Run baseline, candidate, and ablation with the same data, seed, metrics, logs, and parsing scripts.",
        "Analyze the requested bad-case slices and write audit artifacts before any paper conclusion is promoted.",
    ])
    return {
        "experimental_design": initial_experiment or "Compare a minimal proposed variant against one or two baselines on a focused benchmark slice.",
        "feasibility": "Feasible for a first-pass study only after repo/data/env/protocol gates are prepared and audited.",
        "steps": base_steps,
    }


def _fallback_evaluation(round_index: int) -> dict:
    return {
        "round": round_index,
        "evaluation": "The plan is feasible but should make metrics, baselines, risks, and go/no-go criteria more explicit.",
        "weaknesses": ["Metrics are under-specified.", "Failure analysis is too light.", "Ablation design needs sharper controls."],
    }


def _fallback_repair(plan: dict, evaluation: dict) -> dict:
    repaired = dict(plan or {})
    steps = list(repaired.get("steps", []))
    steps.append("Add a checkpoint that validates metrics, baselines, and failure taxonomy before scaling experiments.")
    repaired["steps"] = steps
    repaired["feasibility"] = repaired.get("feasibility", "") + " The repaired version adds clearer validation checkpoints."
    repaired["repair_summary"] = [
        "Clarified the validation checkpoint before scaling experiments.",
        "Made metrics, baselines, and failure analysis more explicit.",
        "Improved feasibility by adding an early go/no-go review.",
    ]
    return repaired


def _new_plan_id(idea: dict) -> str:
    return f"plan-{_idea_key(idea) or 'unknown'}"


def _version_id(plan: dict) -> str:
    return f"v{len(plan.get('versions', [])) + 1}"


def _project_taste_dir() -> Path | None:
    project = (
        os.environ.get("PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("DEFAULT_PROJECT_ID")
        or ""
    ).strip()
    if not project:
        return None
    root = Path(os.environ.get("WORKSPACE_ROOT") or ROOT).expanduser()
    return root / "projects" / project / "planning" / "finding"


def _project_root() -> Path | None:
    project = (
        os.environ.get("PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("DEFAULT_PROJECT_ID")
        or ""
    ).strip()
    if not project:
        return None
    root = Path(os.environ.get("WORKSPACE_ROOT") or ROOT).expanduser()
    return root / "projects" / project


def _payload_run_id(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("run_id") or data.get("source_run_id") or data.get("find_run_id") or data.get("current_find_run_id") or "").strip()

def _project_current_find_run_id(project_root: Path) -> str:
    for rel in (
        Path("state/current_find_research_plan.json"),
        Path("planning/finding/find_results.json"),
        Path("planning/finding/find_progress.json"),
        Path("planning/finding/ideas.json"),
    ):
        data = read_json(project_root / rel, {})
        run_id = _payload_run_id(data)
        if run_id:
            return run_id
    return ""


def _project_sync_allowed_for_run(run_id: str) -> bool:
    project_root = _project_root()
    if project_root is None or not str(run_id or "").strip():
        return False
    return _project_current_find_run_id(project_root) == str(run_id).strip()



def _item_run_id(item: dict) -> str:
    return str(item.get("run_id") or item.get("find_run_id") or item.get("source_run_id") or "").strip()


def _payload_matches_run(data: dict, run_id: str, item_key: str) -> bool:
    payload_run_id = str(data.get("run_id") or data.get("find_run_id") or data.get("source_run_id") or "").strip()
    if payload_run_id:
        return payload_run_id == run_id
    rows = data.get(item_key, []) if isinstance(data, dict) else []
    row_run_ids = {_item_run_id(row) for row in rows if isinstance(row, dict) and _item_run_id(row)}
    return not row_run_ids or row_run_ids == {run_id}


def _same_run_project_json(filename: str, run_id: str, item_key: str) -> dict | None:
    taste_dir = _project_taste_dir()
    if not taste_dir:
        return None
    data = read_json(taste_dir / filename, None)
    if not isinstance(data, dict) or not _payload_matches_run(data, run_id, item_key):
        return None
    return data


def _idea_key(idea: dict) -> str:
    return str(idea.get("id") or idea.get("idea_id") or idea.get("title") or "").strip()


def _plan_idea_key(plan: dict) -> str:
    return str(plan.get("idea_id") or plan.get("id") or plan.get("title") or "").strip()


def _approved_for_planning(idea: dict) -> bool:
    if not isinstance(idea, dict):
        return False
    status = str(idea.get("status") or idea.get("recommendation") or "").strip().lower()
    if status in {"deleted", "rejected", "reject", "archived"}:
        return False
    if idea.get("approved") is True or idea.get("approved_for_planning") is True or idea.get("pursue") is True:
        return True
    return status == "approved" or "approved" in status or "pursue" in status


EXECUTION_TRUE_VALUES = {"1", "true", "yes", "y", "selected", "select", "execute", "execute_next", "primary", "best", "best_idea", "best_plan"}
EXECUTION_FALSE_VALUES = {"0", "false", "no", "n", "rejected", "reject", "skip", "backlog", "candidate_only", "not_selected"}


def _truthy_execution_value(value: object) -> bool:
    if value is True:
        return True
    if value in (False, None, ""):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in EXECUTION_TRUE_VALUES


def _falsey_execution_value(value: object) -> bool:
    if value is False:
        return True
    if value in (None, ""):
        return False
    text = str(value).strip().lower()
    return text in EXECUTION_FALSE_VALUES


def _explicit_execution_selection(item: dict, *, kind: str) -> bool:
    if not isinstance(item, dict):
        return False
    keys = ["selected_for_execution", "execute_next", "primary", "selected"]
    keys.append("best_idea" if kind == "idea" else "best_plan")
    for key in keys:
        value = item.get(key)
        if _truthy_execution_value(value):
            return True
        if _falsey_execution_value(value):
            return False
    selection = item.get("execution_selection") if isinstance(item.get("execution_selection"), dict) else {}
    if selection:
        for key in ("selected", "selected_for_execution", "execute_next", "primary"):
            if _truthy_execution_value(selection.get(key)):
                return True
    decision = str(item.get("execution_decision") or item.get("selection_decision") or "").strip().lower()
    return decision in EXECUTION_TRUE_VALUES


def _numeric_value(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower()
    if not text:
        return default
    mapping = {"very_high": 4.0, "high": 3.0, "medium": 2.0, "med": 2.0, "low": 1.0, "very_low": 0.5}
    if text in mapping:
        return mapping[text]
    try:
        return float(text)
    except ValueError:
        return default


def _execution_rank_score(item: dict) -> float:
    rank = _numeric_value(item.get("execution_rank") or item.get("rank") or item.get("idea_rank") or item.get("plan_rank"), 0.0)
    return 1000.0 - rank if rank > 0 else 0.0


def _execution_score(item: dict, index: int = 0) -> float:
    score = _execution_rank_score(item)
    for key in ("execution_score", "judge_score", "idea_score", "plan_score", "score", "feasibility_score", "evidence_score"):
        score += _numeric_value(item.get(key), 0.0)
    for key in ("evidence_strength", "feasibility", "novelty", "readiness"):
        score += _numeric_value(item.get(key), 0.0)
    return score - index * 0.0001


def _select_execution_item(items: list[dict], *, kind: str) -> tuple[dict | None, str]:
    candidates = [item for item in items if isinstance(item, dict)]
    if not candidates:
        return None, "none"
    explicit = [item for item in candidates if _explicit_execution_selection(item, kind=kind)]
    if not explicit:
        return None, "no_explicit_selection"
    selected = max(enumerate(explicit), key=lambda pair: _execution_score(pair[1], pair[0]))[1]
    return selected, "explicit"


def _plan_id(plan: dict) -> str:
    return str(plan.get("plan_id") or plan.get("id") or "").strip()


def _summarize_selected_item(item: dict | None, *, kind: str) -> dict:
    if not isinstance(item, dict):
        return {}
    keys = ["title", "new_method", "hypothesis", "method_details", "initial_experiment", "inspired_by", "supporting_papers"]
    keys = (["id", "idea_id"] if kind == "idea" else ["plan_id", "idea_id"]) + keys
    if kind != "idea":
        keys.append("status")
    return {key: item.get(key) for key in keys if key in item}


def _apply_execution_selection(ideas: list[dict], plans: list[dict], *, source: str = "taste_plan") -> dict:
    idea_rows = [idea for idea in ideas if isinstance(idea, dict)]
    plan_rows = [plan for plan in plans if isinstance(plan, dict)]
    approved_rows = [idea for idea in idea_rows if _approved_for_planning(idea)]
    explicit_plan_rows = [plan for plan in plan_rows if _explicit_execution_selection(plan, kind="plan")]
    selected_plan: dict | None = explicit_plan_rows[0] if len(explicit_plan_rows) == 1 else None
    selected_by = "claude_or_human_explicit_plan_selection" if selected_plan is not None else "no_explicit_current_find_selection"
    selection_issue = "ambiguous_selected_plan" if len(explicit_plan_rows) > 1 else "missing_selected_plan" if plan_rows else ""
    selected_idea: dict | None = None
    if selected_plan is not None:
        plan_idea_id = str(selected_plan.get("idea_id") or "").strip()
        selected_idea = next((idea for idea in idea_rows if _idea_key(idea) == plan_idea_id), None)
        if selected_idea is None and plan_idea_id:
            selection_issue = "selected_plan_missing_matching_idea"
    selected_idea_id = _idea_key(selected_idea) if isinstance(selected_idea, dict) else str((selected_plan or {}).get("idea_id") or "").strip()
    selected_plan_id = _plan_id(selected_plan) if isinstance(selected_plan, dict) else ""
    if selected_plan is not None and not selected_plan_id:
        selection_issue = "selected_plan_id_missing"
    if selection_issue != "ambiguous_selected_plan":
        for idea in idea_rows:
            idea_selected = bool(selected_idea_id and _idea_key(idea) == selected_idea_id)
            idea["selected_for_execution"] = idea_selected
            idea["execute_next"] = idea_selected
            idea["execution_selection"] = {
                "selected": idea_selected,
                "selected_plan_id": selected_plan_id if idea_selected else "",
                "source": source,
                "selected_by": selected_by if idea_selected else "not_selected_candidate_backlog",
            }
        for plan in plan_rows:
            plan_selected = bool(selected_plan_id and _plan_id(plan) == selected_plan_id)
            plan["selected_for_execution"] = plan_selected
            plan["execute_next"] = plan_selected
            plan["execution_policy"] = {
                "status": "selected_plan_only" if plan_selected else "candidate_backlog_only",
                "downstream_consumes": "selected_plan_id" if plan_selected else "selected plan only; this plan is not executable unless promoted by Claude/human supervision",
                "source": source,
            }
    else:
        for plan in plan_rows:
            plan["execution_policy"] = {
                **(plan.get("execution_policy") if isinstance(plan.get("execution_policy"), dict) else {}),
                "status": "ambiguous_selected_plan",
                "downstream_consumes": "blocked_until_exactly_one_selected_plan_id",
                "source": source,
            }
    return {
        "selected_idea_id": selected_idea_id,
        "selected_plan_id": selected_plan_id,
        "selected_idea": _summarize_selected_item(selected_idea, kind="idea"),
        "selected_plan": _summarize_selected_item(selected_plan, kind="plan"),
        "selected_by": selected_by,
        "selection_issue": selection_issue,
        "execution_policy": {
            "status": "selected_plan_only" if selected_plan_id else (selection_issue or "no_selected_plan"),
            "downstream_consumes": "selected_plan_id",
            "candidate_backlog_policy": "Non-selected ideas/plans remain visible for supervision but must not drive environment, experiment, or paper execution.",
            "selection_issue": selection_issue,
            "source": source,
        },
    }


def _idea_new_method(idea: dict) -> str:
    return str(idea.get("new_method") or idea.get("hypothesis") or "").strip()


def _idea_method_details(idea: dict) -> str:
    return str(idea.get("method_details") or idea.get("mechanism") or idea.get("rationale") or "").strip()


def _idea_initial_experiment(idea: dict) -> str:
    return str(
        idea.get("initial_experiment")
        or idea.get("experiment_design")
        or idea.get("experimental_design")
        or idea.get("min_experiment")
        or idea.get("minimum_experiment")
        or ""
    ).strip()


def _idea_for_planning(idea: dict) -> dict:
    normalized = dict(idea or {})
    new_method = _idea_new_method(normalized)
    method_details = _idea_method_details(normalized)
    initial_experiment = _idea_initial_experiment(normalized)
    if new_method:
        normalized["new_method"] = new_method
        normalized["hypothesis"] = new_method
    if method_details:
        normalized["method_details"] = method_details
        normalized["mechanism"] = method_details
    if initial_experiment:
        normalized["initial_experiment"] = initial_experiment
        normalized["min_experiment"] = initial_experiment
        normalized["minimum_experiment"] = initial_experiment
    return normalized


def _merge_items_by_key(base: list[dict], override: list[dict], key_fn: Callable[[dict], str]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in [*base, *override]:
        if not isinstance(item, dict):
            continue
        key = key_fn(item)
        if not key:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = item
    return [merged[key] for key in order]


def _load_ideas_data(directory: Path, run_id: str) -> dict:
    runtime_data = read_json(directory / "ideas.json", {"run_id": run_id, "ideas": []})
    project_data = _same_run_project_json("ideas.json", run_id, "ideas")
    if not project_data:
        return runtime_data if isinstance(runtime_data, dict) else {"run_id": run_id, "ideas": []}
    runtime_ideas = runtime_data.get("ideas", []) if isinstance(runtime_data, dict) else []
    project_ideas = project_data.get("ideas", [])
    merged = dict(runtime_data) if isinstance(runtime_data, dict) else {"run_id": run_id}
    merged.update({k: v for k, v in project_data.items() if k != "ideas"})
    merged["run_id"] = run_id
    merged["ideas"] = _merge_items_by_key(runtime_ideas, project_ideas, _idea_key)
    return merged


def _load_plans_data(directory: Path, run_id: str) -> dict:
    runtime_data = read_json(directory / "plans.json", {"run_id": run_id, "plans": []})
    project_data = _same_run_project_json("plans.json", run_id, "plans")
    if not project_data:
        return runtime_data if isinstance(runtime_data, dict) else {"run_id": run_id, "plans": []}
    runtime_plans = runtime_data.get("plans", []) if isinstance(runtime_data, dict) else []
    project_plans = project_data.get("plans", [])
    merged = dict(runtime_data) if isinstance(runtime_data, dict) else {"run_id": run_id}
    merged.update({k: v for k, v in project_data.items() if k != "plans"})
    merged["run_id"] = run_id
    merged["plans"] = _merge_items_by_key(runtime_plans, project_plans, lambda plan: str(plan.get("plan_id") or _plan_idea_key(plan)))
    return merged


def _sync_project_plans(run_id: str, data: dict, markdown: str) -> None:
    target_dir = _project_taste_dir()
    project_root = _project_root()
    if target_dir is None or project_root is None:
        return
    if not _project_sync_allowed_for_run(run_id):
        return
    write_json(target_dir / "plans.json", data)
    write_text(target_dir / "plan.md", markdown)

    plans = [row for row in data.get("plans", []) if isinstance(row, dict)]
    now = data.get("human_supervision_updated_at") or datetime.now(timezone.utc).isoformat()
    selection_fields = {
        key: data.get(key)
        for key in ["selected_idea_id", "selected_plan_id", "selected_idea", "selected_plan", "selected_by", "execution_policy"]
        if key in data
    }
    state_path = project_root / "state" / "current_find_research_plan.json"
    state = read_json(state_path, {})
    if isinstance(state, dict) and (_payload_run_id(state) in {"", run_id}):
        state["run_id"] = run_id
        state["plans"] = plans
        state["current_find_plan_count"] = len(plans)
        state.update(selection_fields)
        state["human_supervision_updated_at"] = now
        state["human_supervision_source"] = data.get("human_supervision_source") or "taste_plan_sync"
        write_json(state_path, state)
    bridge_path = project_root / "state" / "taste_plan_bridge.json"
    bridge = read_json(bridge_path, {})
    if isinstance(bridge, dict) and (_payload_run_id(bridge) in {"", run_id}):
        bridge["source"] = data.get("source") or bridge.get("source") or "finding"
        bridge["run_id"] = run_id
        bridge["plans_json"] = data
        bridge["plan_markdown_path"] = str(target_dir / "plan.md")
        bridge["plan_markdown_excerpt"] = markdown[:12000]
        bridge.update(selection_fields)
        bridge["human_supervision_updated_at"] = now
        write_json(bridge_path, bridge)


def _write_plan_outputs(directory: Path, data: dict, ideas: list[dict] | None = None) -> None:
    plans = [row for row in data.get("plans", []) if isinstance(row, dict)]
    data["plans"] = plans
    data.update(_apply_execution_selection(ideas or [], plans, source="taste_auto_plan"))
    write_json(directory / "plans.json", data)
    markdown = render_plan_markdown(data.get("plans", []))
    write_text(directory / "plan.md", markdown)
    sync_latest("auto_plan", "plans.json", directory / "plans.json")
    sync_latest("auto_plan", "plan.md", directory / "plan.md")
    _sync_project_plans(str(data.get("run_id") or ""), data, markdown)


def _generate_initial_plan(idea: dict, config: AppConfig, generator: LLMClient) -> dict:
    initial = _fallback_initial_plan(idea)
    if not generator.enabled:
        return initial
    idea = _idea_for_planning(idea)
    prompt = f"""
Generate the first version of a detailed research plan for this idea.

Return strict JSON:
{{"experimental_design":"...","feasibility":"...","steps":["step 1","step 2"],"risks":["..."],"metrics":["..."]}}

Idea:
{idea}

Researcher profile:
{config.researcher_profile}
"""
    data = generator.json_or_none(prompt)
    return data if isinstance(data, dict) else initial


def _evaluate_plan(plan: dict, idea: dict, round_index: int, config: AppConfig, evaluator: LLMClient) -> dict:
    idea = _idea_for_planning(idea)
    fallback = _fallback_evaluation(round_index)
    if not evaluator.enabled:
        return fallback
    prompt = f"""
Evaluate this research plan. Identify weaknesses and concrete repair instructions.

Return strict JSON:
{{"round":{round_index},"evaluation":"...","weaknesses":["..."],"repair_instructions":["..."]}}

Idea:
{idea}

Plan:
{plan}

Researcher profile:
{config.researcher_profile}
"""
    data = evaluator.json_or_none(prompt)
    if isinstance(data, dict):
        data.setdefault("round", round_index)
        return data
    return fallback


def _repair_plan(plan: dict, idea: dict, evaluation: dict, config: AppConfig, generator: LLMClient) -> dict:
    idea = _idea_for_planning(idea)
    fallback = _fallback_repair(plan, evaluation)
    if not generator.enabled:
        return fallback
    prompt = f"""
Repair and polish this research plan according to the evaluation. Keep useful detail and improve specificity.

Return strict JSON:
{{"experimental_design":"...","feasibility":"...","steps":["step 1","step 2"],"risks":["..."],"metrics":["..."],"repair_summary":["3-6 Chinese bullets describing what changed in this repair round"]}}

Idea:
{idea}

Current plan:
{plan}

Evaluation:
{evaluation}
"""
    data = generator.json_or_none(prompt)
    if isinstance(data, dict):
        data.setdefault("repair_summary", fallback["repair_summary"])
        return data
    return fallback


def _build_version(version_id: str, idea: dict, initial_plan: dict, rounds: int, config: AppConfig, generator: LLMClient, evaluator: LLMClient, should_cancel: CancelFn, log: LogFn) -> dict:
    current = initial_plan
    evaluation_rounds = []
    for round_index in range(1, max(1, rounds) + 1):
        _raise_if_cancelled(should_cancel)
        log(f"Evaluating and repairing {idea.get('title', 'plan')} round {round_index}")
        evaluation = _evaluate_plan(current, idea, round_index, config, evaluator)
        repaired = _repair_plan(current, idea, evaluation, config, generator)
        repair_summary = repaired.pop("repair_summary", None)
        if not repair_summary:
            repair_summary = [
                "Updated the plan according to the evaluator feedback.",
                "Improved specificity of experimental design, feasibility, and validation steps.",
            ]
        evaluation_rounds.append({
            "round": round_index,
            "evaluation": evaluation,
            "repair_summary": repair_summary,
            "repaired_plan": repaired,
        })
        current = repaired
    return {
        "version_id": version_id,
        "initial_plan": initial_plan,
        "evaluation_rounds": evaluation_rounds,
        "final_plan": current,
        "llm": {"generator": generator.summary(), "evaluator": evaluator.summary()},
    }


def run_plan(request: PlanRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    ideas_data = _load_ideas_data(directory, request.run_id)
    approved_ideas = [idea for idea in ideas_data.get("ideas", []) if _approved_for_planning(idea)]
    ideas = approved_ideas
    if request.idea_ids:
        allowed = set(request.idea_ids)
        ideas = [idea for idea in approved_ideas if _idea_key(idea) in allowed]
    if not ideas:
        log("No approved ideas selected for planning.")
        empty = {"run_id": request.run_id, "plans": []}
        _write_plan_outputs(directory, empty, ideas=[])
        update_manifest(directory, "plan")
        return {"run_id": request.run_id, "plans": []}

    generator = LLMClient(config, "plan_generator")
    evaluator = LLMClient(config, "plan_evaluator")
    if os.environ.get("PLAN_USE_LLM", "1").lower() in {"0", "false", "no"}:
        generator.enabled = False
        evaluator.enabled = False
    plans = []
    for idea in ideas:
        _raise_if_cancelled(should_cancel)
        idea = _idea_for_planning(idea)
        log(f"Planning idea: {idea.get('title', 'Untitled')}")
        initial_plan = _generate_initial_plan(idea, config, generator)
        plan = {
            "plan_id": _new_plan_id(idea),
            "idea_id": _idea_key(idea),
            "title": idea.get("title", "Untitled"),
            "hypothesis": _idea_new_method(idea),
            "new_method": _idea_new_method(idea),
            "method_details": _idea_method_details(idea),
            "initial_experiment": _idea_initial_experiment(idea),
            "inspired_by": idea.get("inspired_by") or idea.get("supporting_papers") or [],
            "supporting_papers": idea.get("supporting_papers") or [],
            "completed": False,
            "completed_at": "",
            "versions": [],
        }
        plan["versions"].append(_build_version("v1", idea, initial_plan, request.repair_rounds, config, generator, evaluator, should_cancel, log))
        plans.append(plan)

    _raise_if_cancelled(should_cancel)
    data = {"run_id": request.run_id, "plans": plans}
    _write_plan_outputs(directory, data, ideas=ideas)
    update_manifest(directory, "plan")
    log("Plan stage complete")
    return data


def polish_plan(request: PlanPolishRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    data = _load_plans_data(directory, request.run_id)
    ideas_data = _load_ideas_data(directory, request.run_id)
    ideas_by_id = {_idea_key(idea): idea for idea in ideas_data.get("ideas", []) if _idea_key(idea)}
    generator = LLMClient(config, "plan_generator")
    evaluator = LLMClient(config, "plan_evaluator")
    if os.environ.get("PLAN_USE_LLM", "1").lower() in {"0", "false", "no"}:
        generator.enabled = False
        evaluator.enabled = False
    for plan in data.get("plans", []):
        if plan.get("plan_id") != request.plan_id:
            continue
        versions = plan.setdefault("versions", [])
        if not versions:
            continue
        base = next((version for version in versions if version.get("version_id") == request.version_id), None) if request.version_id else versions[-1]
        if not base:
            base = versions[-1]
        idea = _idea_for_planning(ideas_by_id.get(plan.get("idea_id"), {"id": plan.get("idea_id"), "title": plan.get("title"), "hypothesis": plan.get("hypothesis"), "new_method": plan.get("new_method"), "method_details": plan.get("method_details"), "initial_experiment": plan.get("initial_experiment")}))
        version = _build_version(_version_id(plan), idea, base.get("final_plan", {}), request.rounds, config, generator, evaluator, should_cancel, log)
        versions.append(version)
        plan["completed"] = False
        plan["completed_at"] = ""
        break
    _write_plan_outputs(directory, data, ideas=list(ideas_by_id.values()))
    update_manifest(directory, "plan")
    return data


def finish_plan(run_id: str, plan_id: str) -> dict:
    directory = run_dir(run_id)
    data = _load_plans_data(directory, run_id)
    for plan in data.get("plans", []):
        if plan.get("plan_id") != plan_id:
            continue
        if not plan.get("completed"):
            plan["completed"] = True
            plan["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_plan_outputs(directory, data)
        update_manifest(directory, "plan")
        return data
    raise ValueError(f"Plan not found: {plan_id}")


def _append_plan_section(lines: list[str], title: str, value: object) -> None:
    if value is None or value == "":
        return
    lines.extend(["", f"### {title}", ""])
    if isinstance(value, list):
        for item in value:
            lines.append(f"- {item}")
    else:
        lines.append(str(value))


def _generic_plan_steps(steps: object) -> bool:
    if not isinstance(steps, list) or not steps:
        return True
    joined = "\n".join(str(row or "") for row in steps).lower()
    generic_markers = [
        "verify current find run_id",
        "environment-stage claude code reads",
        "accept a base only by writing",
        "evidence_ready_repo_selection.json",
        "refresh reference/scientific/evidence/submission gates",
        "run minimal baseline/candidate/ablation experiments",
    ]
    hits = sum(1 for marker in generic_markers if marker in joined)
    specific_markers = ["prefergrow", "disco", "rl-diffrec", "lsg-diff", "movielens", "amazon-beauty", "ndcg@", "hr@"]
    return hits >= 2 and not any(marker in joined for marker in specific_markers)


def _specific_plan_steps(initial_experiment: str, new_method: str) -> list[str]:
    steps: list[str] = []
    if initial_experiment:
        steps.append(f"Use this initial experiment as the execution contract: {initial_experiment}")
    if new_method:
        steps.append(f"Implement the smallest testable change for the proposed method: {new_method}")
    steps.extend([
        "Verify repo/data/protocol gates before running commands; keep the plan blocked if environment evidence is missing.",
        "Run baseline, candidate, and ablation with the same data, seed, metrics, logs, and parsing scripts.",
        "Analyze the requested bad-case slices and write audit artifacts before any paper conclusion is promoted.",
    ])
    return steps


def _compact_plan_text(value: object, limit: int = 1200) -> str:
    text = " ".join(str(value or "").replace("\r", "\n").split())
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _plan_refs_text(refs: object) -> list[str]:
    if not isinstance(refs, list):
        return []
    out: list[str] = []
    for ref in refs[:8]:
        if isinstance(ref, dict):
            title = _compact_plan_text(ref.get("title") or ref.get("name") or ref.get("paper_title"), 180)
            reason = _compact_plan_text(ref.get("reason") or ref.get("evidence_role") or ref.get("source") or ref.get("venue"), 260)
            url = _compact_plan_text(ref.get("url") or ref.get("pdf_url"), 220)
            suffix = ""
            if reason:
                suffix += f": {reason}"
            if url:
                suffix += f" ({url})"
            out.append(f"{title}{suffix}" if title else suffix.strip())
        elif ref:
            out.append(_compact_plan_text(ref, 240))
    return [item for item in out if item]


def render_plan_markdown(plans: list[dict]) -> str:
    lines = ["# Research Plans", ""]
    selected = next((plan for plan in plans if isinstance(plan, dict) and plan.get("selected_for_execution") is True), None)
    if selected:
        lines.extend([
            "## Selected Plan for Execution", "",
            f"- **Plan ID**: `{selected.get('plan_id', '')}`",
            f"- **Idea ID**: `{selected.get('idea_id', '')}`",
            f"- **Title**: {selected.get('title', 'Untitled')}",
            "- **Policy**: downstream environment, experiment, and paper execution must consume only this selected plan until Claude/human supervision changes the selection.", "",
        ])
    for index, plan in enumerate(plans, 1):
        versions = plan.get("versions", [])
        latest = versions[-1] if versions else {}
        final_plan = latest.get("final_plan", {}) or {
            "experimental_design": plan.get("initial_experiment") or plan.get("experimental_design") or plan.get("experiment_design") or plan.get("minimum_experiment") or plan.get("min_experiment") or plan.get("evidence_policy") or "",
            "feasibility": plan.get("feasibility") or plan.get("go_no_go") or "",
            "steps": plan.get("steps") or [],
            "risks": plan.get("risks") or plan.get("risk") or plan.get("limitations") or [],
            "metrics": plan.get("metrics") or plan.get("success_gate") or [],
        }
        if isinstance(final_plan.get("steps"), str):
            final_plan["steps"] = [final_plan["steps"]]
        initial_experiment = _compact_plan_text(plan.get("initial_experiment") or final_plan.get("experimental_design"), 1800)
        new_method = _compact_plan_text(plan.get("new_method") or plan.get("hypothesis"), 1800)
        method_details = _compact_plan_text(plan.get("method_details") or plan.get("mechanism"), 1800)
        steps = list(final_plan.get("steps", []) or [])
        if _generic_plan_steps(steps):
            steps = _specific_plan_steps(initial_experiment, new_method)
        lines.extend([
            f"## {index}. {plan.get('title', 'Untitled')}",
            "",
            f"- **Plan ID**: `{plan.get('plan_id', '')}`",
            f"- **Idea ID**: `{plan.get('idea_id', '')}`",
            f"- **Latest Version**: `{latest.get('version_id') or latest.get('version') or ''}`",
            f"- **Selected for Execution**: {bool(plan.get('selected_for_execution'))}",
            f"- **Completed**: {bool(plan.get('completed'))}",
            "",
            "### New Method",
            new_method,
        ])
        if method_details:
            lines.extend(["", "### Method Details", method_details])
        lines.extend(["", "### Initial Experiment", initial_experiment or "Pending project-agent completion from the current readings.", ""])
        refs = _plan_refs_text(plan.get("inspired_by") or plan.get("supporting_papers") or plan.get("positive_anchor_papers"))
        if refs:
            lines.extend(["### Inspired by", ""])
            for ref in refs:
                lines.append(f"- {ref}")
            lines.append("")
        lines.extend(["### Step-by-step Plan"])
        for step_index, step in enumerate(steps, 1):
            lines.append(f"{step_index}. {_compact_plan_text(step, 900)}")
        _append_plan_section(lines, "Risks", final_plan.get("risks"))
        _append_plan_section(lines, "Metrics", final_plan.get("metrics"))
        if not plan.get("completed"):
            lines.extend(["", "### Evaluation / Repair Rounds", ""])
            for round_item in latest.get("evaluation_rounds", []):
                evaluation = round_item.get("evaluation", {})
                weaknesses = evaluation.get("weaknesses", []) if isinstance(evaluation, dict) else []
                repair_instructions = evaluation.get("repair_instructions", []) if isinstance(evaluation, dict) else []
                repair_summary = round_item.get("repair_summary", [])
                lines.extend([
                    f"#### Round {round_item.get('round', '')}",
                    "",
                    f"- **Evaluation**: {evaluation.get('evaluation', evaluation) if isinstance(evaluation, dict) else evaluation}",
                    f"- **Weaknesses**: {', '.join(weaknesses) if isinstance(weaknesses, list) else weaknesses}",
                    f"- **Repair Instructions**: {', '.join(repair_instructions) if isinstance(repair_instructions, list) else repair_instructions}",
                    f"- **Repair Summary**: {', '.join(repair_summary) if isinstance(repair_summary, list) else repair_summary}",
                    "",
                ])
        if len(versions) > 1:
            lines.extend(["", "### Version History", ""])
            for version in versions:
                lines.append(f"- `{version.get('version_id')}`: {len(version.get('evaluation_rounds', []))} evaluation/repair rounds")
    return "\n".join(lines).rstrip() + "\n"
