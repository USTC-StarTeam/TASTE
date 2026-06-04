from __future__ import annotations

import json
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


def _fallback_initial_plan(idea: dict, references: list[dict] | None = None) -> dict:
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
        "code_and_method_references": references or [],
    }


def _main_agent_plan_config(config: AppConfig) -> AppConfig:
    configured = config.model_copy(deep=True)
    main_role = configured.llm_roles.get("idea_generator") or configured.llm_roles.get("read")
    if not main_role:
        return configured
    for role in ("plan_generator", "plan_evaluator"):
        override = configured.llm_roles.get(role)
        if not override or not override.provider:
            configured.llm_roles[role] = main_role.model_copy(deep=True)
    return configured


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


def _planning_context(directory) -> dict:
    profile_result = read_json(existing_stage_path(directory, "find", "stage0_profile.json"), {})
    find_results = read_json(existing_stage_path(directory, "find", "find_results.json"), {})
    read_results = read_json(existing_stage_path(directory, "read", "read_results.json"), {})
    ideas_data = read_json(existing_stage_path(directory, "idea", "ideas.json"), {})
    readings = [
        item.get("content", {})
        for item in read_results.get("readings", [])
        if isinstance(item, dict) and isinstance(item.get("content"), dict)
    ]
    discovery_sources = []
    for source_name in ("github", "articles", "nature", "science", "huggingface"):
        for item in find_results.get(source_name, []) if isinstance(find_results, dict) else []:
            if isinstance(item, dict):
                discovery_sources.append({
                    "source": source_name,
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "summary": item.get("reason") or item.get("abstract", "")[:500],
                })
    return {
        "normalized_researcher_profile": profile_result.get("profile", {}) if isinstance(profile_result, dict) else {},
        "discovery_sources": discovery_sources,
        "main_agent_summary": read_results.get("main_agent_summary") or read_results.get("cross_summary", {}),
        "method_analysis": read_results.get("method_analysis", {}),
        "readings": readings,
        "method_evaluation": ideas_data.get("method_evaluation", {}) if isinstance(ideas_data, dict) else {},
    }


def _normalize_references(items: object) -> list[dict]:
    references = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        reference = {
            "source_type": str(item.get("source_type", "unresolved")),
            "paper_or_project": str(item.get("paper_or_project", "")),
            "url": str(item.get("url", "")),
            "version": str(item.get("version", "")),
            "location": str(item.get("location", "")),
            "reuse": str(item.get("reuse", "")),
            "modification": str(item.get("modification", "")),
            "evidence": str(item.get("evidence", "")),
            "verification_status": str(item.get("verification_status", "unresolved")).lower(),
            "verification_evidence": str(item.get("verification_evidence", "")),
        }
        if reference["verification_status"] == "verified" and not (
            reference["url"] and reference["location"] and reference["verification_evidence"]
        ):
            reference["verification_status"] = "unresolved"
        references.append(reference)
    return references


def _fallback_references(idea: dict) -> list[dict]:
    return _normalize_references([
        {
            "source_type": source.get("source", "paper") if isinstance(source, dict) else "paper",
            "paper_or_project": source.get("title", "") if isinstance(source, dict) else str(source),
            "url": source.get("url", "") if isinstance(source, dict) else "",
            "reuse": "Potential foundation cited by the selected idea.",
            "modification": "Inspect the source and identify the exact reusable component before implementation.",
            "evidence": "Listed in the selected idea's inspired_by evidence.",
            "verification_status": "unresolved",
            "verification_evidence": "Exact repository version and code location have not been inspected.",
        }
        for source in idea.get("inspired_by", [])
    ])


def _collect_references(run_id: str, idea: dict, context: dict, config: AppConfig, log: LogFn) -> tuple[list[dict], dict]:
    worker = LLMClient(
        config,
        "plan_generator",
        conversation_key=f"run:{run_id}:worker:auto_plan:references:{idea.get('id', 'unknown')}",
        persist_session=False,
        tools="WebSearch,WebFetch,Read,Glob,Grep",
    )
    fallback = _fallback_references(idea)
    if not worker.enabled:
        return fallback, worker.summary()
    prompt = f"""
You are a disposable reference-collection worker supporting a main research agent.
Find concrete code and method foundations for the selected idea. Inspect cited repositories or official project sources when available.
Do not write the experimental plan.

Selected idea:
{json.dumps(idea, ensure_ascii=False, indent=2)}

Prior-stage research context:
{json.dumps(context, ensure_ascii=False, indent=2)}

Return one strict JSON object:
{{
  "references": [
    {{
      "source_type": "repository|paper|dataset|benchmark|unresolved",
      "paper_or_project": "specific name",
      "url": "specific source URL",
      "version": "commit, tag, release, or empty",
      "location": "exact file/module/class/function, or empty",
      "reuse": "what should be reused",
      "modification": "what should be added or changed",
      "evidence": "why this source matches the idea",
      "verification_status": "verified|unresolved",
      "verification_evidence": "what you inspected to verify the version and location"
    }}
  ]
}}

Mark a reference verified only when you inspected enough source material to substantiate its URL and exact location.
Never invent repository paths, symbols, versions, or URLs. Keep useful but unverified candidates as unresolved.
"""
    result = worker.json_or_error(prompt)
    data = result.get("data")
    if result.get("ok") and isinstance(data, dict):
        references = _normalize_references(data.get("references"))
        if references:
            log(f"Reference worker collected {len(references)} sources for {idea.get('title', 'plan')}")
            return references, worker.summary()
    log(f"Reference worker unavailable for {idea.get('title', 'plan')}: {str(result.get('error') or '')[:300]}")
    return fallback, worker.summary()


def _generate_initial_plan(idea: dict, references: list[dict], context: dict, config: AppConfig, generator: LLMClient) -> dict:
    initial = _fallback_initial_plan(idea, references)
    if not generator.enabled:
        return initial
    prompt = f"""
Continue as the persistent main research agent. Generate a preliminary experimental plan grounded in the supplied references.

Return strict JSON:
{{
  "hypothesis_and_success_criteria":"...",
  "experimental_design":"...",
  "feasibility":"...",
  "baselines_and_comparisons":["..."],
  "datasets_and_preprocessing":["..."],
  "code_and_method_references":[
    {{
      "source_type":"...",
      "paper_or_project":"...",
      "url":"...",
      "version":"...",
      "location":"...",
      "reuse":"...",
      "modification":"...",
      "evidence":"...",
      "verification_status":"verified|unresolved",
      "verification_evidence":"..."
    }}
  ],
  "implementation_changes":["..."],
  "experiment_matrix_and_ablations":["..."],
  "metrics":["..."],
  "compute_and_resources":["..."],
  "risks":["..."],
  "fallback_options":["..."],
  "steps":["ordered minimum viable experiment step"]
}}

Idea:
{json.dumps(idea, ensure_ascii=False, indent=2)}

Reference foundation:
{json.dumps(references, ensure_ascii=False, indent=2)}

Prior-stage research context:
{json.dumps(context, ensure_ascii=False, indent=2)}

Researcher profile:
{config.researcher_profile}

For every implementation claim, distinguish direct reuse, adaptation, and new code.
Use exact repository version and code location only when the reference is marked verified.
Preserve unresolved references as unresolved inspection tasks. Never invent paths, functions, commits, datasets, or metrics.
"""
    data = generator.json_or_none(prompt)
    if isinstance(data, dict):
        data["code_and_method_references"] = references
        return data
    return initial


def _evaluate_plan(plan: dict, idea: dict, round_index: int, config: AppConfig, evaluator: LLMClient) -> dict:
    fallback = _fallback_evaluation(round_index)
    if not evaluator.enabled:
        return fallback
    prompt = f"""
Evaluate this research plan. Identify weaknesses and concrete repair instructions.
Check whether implementation references are specific, verified where claimed, and clearly distinguish reuse, adaptation, and new code.
Treat invented or unsupported repository paths, symbols, versions, datasets, or metrics as critical weaknesses.

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
Preserve the full plan schema and reference foundation.
Never upgrade an unresolved reference to verified or invent repository paths, symbols, versions, datasets, or metrics.

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
        data["code_and_method_references"] = plan.get("code_and_method_references", [])
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

    main_agent_key = f"run:{request.run_id}:main"
    plan_config = _main_agent_plan_config(config)
    generator = LLMClient(plan_config, "plan_generator", conversation_key=main_agent_key, resume_session=True)
    evaluator = LLMClient(plan_config, "plan_evaluator", conversation_key=main_agent_key, resume_session=True)
    context = _planning_context(directory)
    plans = []
    for idea in ideas:
        _raise_if_cancelled(should_cancel)
        log(f"Planning idea: {idea.get('title', 'Untitled')}")
        references, reference_worker = _collect_references(request.run_id, idea, context, plan_config, log)
        _raise_if_cancelled(should_cancel)
        initial_plan = _generate_initial_plan(idea, references, context, plan_config, generator)
        plan = {
            "plan_id": _new_plan_id(idea),
            "idea_id": idea.get("id", ""),
            "title": idea.get("title", "Untitled"),
            "hypothesis": idea.get("hypothesis", ""),
            "reference_foundation": references,
            "reference_worker": reference_worker,
            "completed": False,
            "completed_at": "",
            "versions": [],
        }
        plan["versions"].append(_build_version("v1", idea, initial_plan, request.repair_rounds, plan_config, generator, evaluator, should_cancel, log))
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
    main_agent_key = f"run:{request.run_id}:main"
    plan_config = _main_agent_plan_config(config)
    generator = LLMClient(plan_config, "plan_generator", conversation_key=main_agent_key, resume_session=True)
    evaluator = LLMClient(plan_config, "plan_evaluator", conversation_key=main_agent_key, resume_session=True)
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
        version = _build_version(_version_id(plan), idea, base.get("final_plan", {}), request.rounds, plan_config, generator, evaluator, should_cancel, log)
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


def _append_reference_section(lines: list[str], references: list[dict]) -> None:
    if not references:
        return
    lines.extend(["", "### Code and Method References", ""])
    for reference in references:
        location = reference.get("location") or "Unresolved"
        version = reference.get("version") or "Unresolved"
        lines.extend([
            f"#### {reference.get('paper_or_project') or 'Unresolved reference'}",
            "",
            f"- **Status**: {reference.get('verification_status', 'unresolved')}",
            f"- **Source**: {reference.get('url') or 'Unresolved'}",
            f"- **Version**: {version}",
            f"- **Location**: `{location}`",
            f"- **Reuse**: {reference.get('reuse', '')}",
            f"- **Modification**: {reference.get('modification', '')}",
            f"- **Evidence**: {reference.get('evidence', '')}",
            f"- **Verification Evidence**: {reference.get('verification_evidence', '')}",
            "",
        ])


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
        ])
        _append_plan_section(lines, "Hypothesis and Success Criteria", final_plan.get("hypothesis_and_success_criteria"))
        _append_plan_section(lines, "Experimental Design", final_plan.get("experimental_design"))
        _append_plan_section(lines, "Feasibility", final_plan.get("feasibility"))
        _append_plan_section(lines, "Baselines and Comparisons", final_plan.get("baselines_and_comparisons"))
        _append_plan_section(lines, "Datasets and Preprocessing", final_plan.get("datasets_and_preprocessing"))
        _append_reference_section(lines, final_plan.get("code_and_method_references") or plan.get("reference_foundation", []))
        _append_plan_section(lines, "Implementation Changes", final_plan.get("implementation_changes"))
        _append_plan_section(lines, "Experiment Matrix and Ablations", final_plan.get("experiment_matrix_and_ablations"))
        _append_plan_section(lines, "Compute and Resources", final_plan.get("compute_and_resources"))
        _append_plan_section(lines, "Fallback Options", final_plan.get("fallback_options"))
        lines.extend(["", "### Step-by-step Plan", ""])
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
