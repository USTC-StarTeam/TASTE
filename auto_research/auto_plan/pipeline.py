from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient
from auto_research.models import AppConfig, PlanPolishRequest, PlanRequest
from auto_research.storage import existing_stage_path, read_json, run_dir, stage_dir, sync_latest, update_manifest, write_json, write_text


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _fallback_initial_plan(idea: dict) -> dict:
    base_steps = [
        "Define the target task, success metrics, and evaluation data slice.",
        "Reproduce the strongest simple baseline and record cost/quality tradeoffs.",
        "Implement the proposed method as the smallest controllable prototype.",
        "Run ablations that isolate the claimed contribution.",
        "Analyze failures and decide whether the idea deserves a larger study.",
    ]
    return {
        "experimental_design": "Compare a minimal proposed variant against one or two baselines on a focused benchmark slice.",
        "feasibility": "Feasible for a first-pass study if data access and evaluation scripts are prepared early.",
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
    return f"plan-{idea.get('id', 'unknown')}"


def _version_id(plan: dict) -> str:
    return f"v{len(plan.get('versions', [])) + 1}"


def _generate_initial_plan(idea: dict, config: AppConfig, generator: LLMClient) -> dict:
    initial = _fallback_initial_plan(idea)
    if not generator.enabled:
        return initial
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
    plan_dir = stage_dir(directory, "plan")
    ideas_data = read_json(existing_stage_path(directory, "idea", "ideas.json"), {"ideas": []})
    approved_ideas = [idea for idea in ideas_data.get("ideas", []) if idea.get("status") == "approved"]
    ideas = approved_ideas
    if request.idea_ids:
        allowed = set(request.idea_ids)
        ideas = [idea for idea in approved_ideas if idea.get("id") in allowed]
    if not ideas:
        log("No approved ideas selected for planning.")
        write_json(plan_dir / "plans.json", {"run_id": request.run_id, "plans": []})
        write_text(plan_dir / "plan.md", "# Research Plans\n\nNo approved ideas selected for planning.\n")
        update_manifest(directory, "plan")
        return {"run_id": request.run_id, "plans": []}

    generator = LLMClient(config, "plan_generator")
    evaluator = LLMClient(config, "plan_evaluator")
    plans = []
    for idea in ideas:
        _raise_if_cancelled(should_cancel)
        log(f"Planning idea: {idea.get('title', 'Untitled')}")
        initial_plan = _generate_initial_plan(idea, config, generator)
        plan = {
            "plan_id": _new_plan_id(idea),
            "idea_id": idea.get("id", ""),
            "title": idea.get("title", "Untitled"),
            "hypothesis": idea.get("hypothesis", ""),
            "completed": False,
            "completed_at": "",
            "versions": [],
        }
        plan["versions"].append(_build_version("v1", idea, initial_plan, request.repair_rounds, config, generator, evaluator, should_cancel, log))
        plans.append(plan)

    _raise_if_cancelled(should_cancel)
    write_json(plan_dir / "plans.json", {"run_id": request.run_id, "plans": plans})
    write_text(plan_dir / "plan.md", render_plan_markdown(plans))
    sync_latest("auto_plan", "plan.md", plan_dir / "plan.md")
    update_manifest(directory, "plan")
    log("Plan stage complete")
    return {"run_id": request.run_id, "plans": plans}


def polish_plan(request: PlanPolishRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    plan_dir = stage_dir(directory, "plan")
    data = read_json(existing_stage_path(directory, "plan", "plans.json"), {"run_id": request.run_id, "plans": []})
    ideas_data = read_json(existing_stage_path(directory, "idea", "ideas.json"), {"ideas": []})
    ideas_by_id = {idea.get("id"): idea for idea in ideas_data.get("ideas", [])}
    generator = LLMClient(config, "plan_generator")
    evaluator = LLMClient(config, "plan_evaluator")
    for plan in data.get("plans", []):
        if plan.get("plan_id") != request.plan_id:
            continue
        versions = plan.setdefault("versions", [])
        if not versions:
            continue
        base = next((version for version in versions if version.get("version_id") == request.version_id), None) if request.version_id else versions[-1]
        if not base:
            base = versions[-1]
        idea = ideas_by_id.get(plan.get("idea_id"), {"id": plan.get("idea_id"), "title": plan.get("title"), "hypothesis": plan.get("hypothesis")})
        version = _build_version(_version_id(plan), idea, base.get("final_plan", {}), request.rounds, config, generator, evaluator, should_cancel, log)
        versions.append(version)
        plan["completed"] = False
        plan["completed_at"] = ""
        break
    write_json(plan_dir / "plans.json", data)
    write_text(plan_dir / "plan.md", render_plan_markdown(data.get("plans", [])))
    sync_latest("auto_plan", "plan.md", plan_dir / "plan.md")
    update_manifest(directory, "plan")
    return data


def finish_plan(run_id: str, plan_id: str) -> dict:
    directory = run_dir(run_id)
    plan_dir = stage_dir(directory, "plan")
    data = read_json(existing_stage_path(directory, "plan", "plans.json"), {"run_id": run_id, "plans": []})
    for plan in data.get("plans", []):
        if plan.get("plan_id") != plan_id:
            continue
        if not plan.get("completed"):
            plan["completed"] = True
            plan["completed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        write_json(plan_dir / "plans.json", data)
        write_text(plan_dir / "plan.md", render_plan_markdown(data.get("plans", [])))
        sync_latest("auto_plan", "plan.md", plan_dir / "plan.md")
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


def render_plan_markdown(plans: list[dict]) -> str:
    lines = ["# Research Plans", ""]
    for index, plan in enumerate(plans, 1):
        versions = plan.get("versions", [])
        latest = versions[-1] if versions else {}
        final_plan = latest.get("final_plan", {}) or {}
        lines.extend([
            f"## {index}. {plan.get('title', 'Untitled')}",
            "",
            f"- **Plan ID**: `{plan.get('plan_id', '')}`",
            f"- **Idea ID**: `{plan.get('idea_id', '')}`",
            f"- **Latest Version**: `{latest.get('version_id', '')}`",
            f"- **Completed**: {bool(plan.get('completed'))}",
            "",
            "### Hypothesis",
            plan.get("hypothesis", ""),
            "",
            "### Experimental Design",
            final_plan.get("experimental_design", ""),
            "",
            "### Feasibility",
            final_plan.get("feasibility", ""),
            "",
            "### Step-by-step Plan",
        ])
        for step_index, step in enumerate(final_plan.get("steps", []), 1):
            lines.append(f"{step_index}. {step}")
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
