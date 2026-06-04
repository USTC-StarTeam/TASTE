from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient, clamp_workers
from auto_research.models import AppConfig, IdeaPatch, IdeaRequest
from auto_research.storage import existing_stage_path, read_json, run_dir, stage_dir, sync_latest, update_manifest, write_json, write_text


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _source_items(directory) -> list[dict]:
    find_results = read_json(existing_stage_path(directory, "find", "find_results.json"), {})
    read_results = read_json(existing_stage_path(directory, "read", "read_results.json"), {})
    items = []
    for source_name in ("articles", "nature", "science", "huggingface", "github"):
        for item in find_results.get(source_name, []):
            items.append({
                "source": source_name,
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "summary": item.get("reason") or item.get("abstract", "")[:500],
            })
    for item in read_results.get("readings", []):
        content = item.get("content", {}) if isinstance(item, dict) else {}
        if not isinstance(content, dict):
            content = {}
        title = content.get("title", item.get("title", "") if isinstance(item, dict) else "")
        summary = content.get("summary", item.get("summary", "") if isinstance(item, dict) else "")
        items.append({
            "source": "read",
            "title": title,
            "url": "",
            "summary": summary,
        })
    cross_summary = read_results.get("main_agent_summary") or read_results.get("cross_summary", {})
    if isinstance(cross_summary, dict):
        summary = " ".join(str(cross_summary.get(key, "")) for key in ["overview", "common_themes", "method_comparison", "limitations_comparison", "next_stage_notes"]).strip()
        if summary:
            items.append({
                "source": "main_agent_summary",
                "title": "Main agent synthesis",
                "url": "",
                "summary": summary,
            })
    method_analysis = read_results.get("method_analysis", {})
    if isinstance(method_analysis, dict):
        pros_cons = method_analysis.get("pros_cons", [])
        summary = " ".join([
            str(method_analysis.get("summary", "")),
            str(method_analysis.get("method_differences", "")),
            str(pros_cons),
        ]).strip()
        if summary:
            items.append({
                "source": "method_analysis",
                "title": "Method cross-comparison",
                "url": "",
                "summary": summary,
            })
    return items


def _fallback_ideas(items: list[dict], max_ideas: int) -> list[dict]:
    seeds = items[: max(2, max_ideas * 2)]
    ideas = []
    for index in range(max_ideas):
        related = seeds[index : index + 2] or seeds[:2]
        title_bits = [item.get("title", "Research Signal").split(":")[0][:60] for item in related]
        ideas.append({
            "id": f"idea-{index + 1:03d}",
            "title": f"Combine signals from {index + 1}: {title_bits[0] if title_bits else 'new research direction'}",
            "hypothesis": "A targeted combination of the selected papers/tools can produce a measurable improvement on the user's research problem.",
            "min_experiment": "Build a small benchmark slice, implement one baseline and one proposed variant, then compare quality, cost, and failure cases.",
            "novelty": "MEDIUM",
            "feasibility": "HIGH",
            "score": round(8.0 - index * 0.3, 2),
            "status": "pending",
            "inspired_by": [{"title": item.get("title", ""), "source": item.get("source", ""), "url": item.get("url", "")} for item in related],
        })
    return ideas


def _dedupe_ideas(ideas: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for idea in ideas:
        key = " ".join(str(idea.get("title", "")).lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(idea)
    return result


def _score_value(item: dict) -> float:
    try:
        return float(item.get("score") or item.get("judge_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _evaluate_methods(directory, main_agent: LLMClient, log: LogFn) -> dict:
    if not main_agent.enabled:
        return {}
    profile_result = read_json(existing_stage_path(directory, "find", "stage0_profile.json"), {})
    read_results = read_json(existing_stage_path(directory, "read", "read_results.json"), {})
    profile = profile_result.get("profile", {}) if isinstance(profile_result, dict) else {}
    readings = read_results.get("readings", []) if isinstance(read_results, dict) else []
    if not isinstance(profile, dict) or not isinstance(readings, list) or not readings:
        return {}
    content_only = [
        reading.get("content", {})
        for reading in readings
        if isinstance(reading, dict) and isinstance(reading.get("content"), dict)
    ]
    if not content_only:
        return {}
    context = {
        "main_agent_summary": read_results.get("main_agent_summary") or read_results.get("cross_summary", {}),
        "method_analysis": read_results.get("method_analysis", {}),
        "readings": content_only,
    }
    prompt = f"""
Continue as the main research agent from the auto_read stage.
Evaluate the methods in the structured reading results against the normalized researcher profile.
Do not generate research ideas or experimental plans yet.

Normalized researcher profile:
{json.dumps(profile, ensure_ascii=False, indent=2)}

Structured auto_read results:
{json.dumps(context, ensure_ascii=False, indent=2)}

Return one strict JSON object:
{{
  "summary": "overall assessment",
  "methods": [
    {{
      "title": "paper or method title",
      "profile_alignment": "assessment",
      "reusable_contribution": "assessment",
      "evidence_strength": "assessment",
      "feasibility": "assessment",
      "required_resources": "assessment",
      "limitations_and_risks": "assessment",
      "extension_opportunities": "assessment",
      "combination_opportunities": "assessment"
    }}
  ],
  "cross_method_assessment": "comparison across methods",
  "recommended_focus": ["method-level direction worth prioritizing"]
}}
Use Chinese. Base the evaluation only on the normalized profile and structured reading evidence.
"""
    result = main_agent.json_or_error(prompt)
    data = result.get("data")
    if result.get("ok") and isinstance(data, dict):
        log("Main agent method evaluation accepted")
        return data
    log(f"Main agent method evaluation unavailable: {str(result.get('error') or '')[:300]}")
    return {}


def _generate_worker_candidates(
    run_id: str,
    config: AppConfig,
    workers: int,
    candidate_count: int,
    profile: dict,
    method_evaluation: dict,
    evidence: dict,
    log: LogFn,
) -> tuple[list[dict], list[dict]]:
    perspectives = [
        "Extend weaknesses and limitations into research opportunities.",
        "Combine complementary methods into a coherent new direction.",
        "Adapt the methods to the researcher's target workflow and constraints.",
        "Develop evaluation or benchmark-centered research directions.",
        "Explore high-risk, high-novelty directions grounded in the evidence.",
    ][:workers]

    def run_worker(worker_index: int, perspective: str) -> tuple[list[dict], dict]:
        worker = LLMClient(
            config,
            "idea_generator",
            conversation_key=f"run:{run_id}:worker:auto_idea:{worker_index}",
            persist_session=False,
        )
        prompt = f"""
You are an idea-exploration worker supporting a main research agent.

Explore only this perspective:
{perspective}

Normalized researcher profile:
{json.dumps(profile, ensure_ascii=False, indent=2)}

Main-agent method evaluation:
{json.dumps(method_evaluation, ensure_ascii=False, indent=2)}

Relevant research evidence:
{json.dumps(evidence, ensure_ascii=False, indent=2)}

Generate {candidate_count} distinct research idea candidates.
Do not select final ideas or create detailed experimental plans.

Each idea must directly match the normalized researcher profile, identify the evidence or limitation that motivates it, state a testable hypothesis, explain its novelty relative to the supplied methods, and include only a brief feasibility note.

Return one strict JSON array:
[
  {{
    "title": "Chinese title",
    "hypothesis": "testable Chinese hypothesis",
    "motivation": "evidence-backed motivation",
    "novelty": "specific novelty",
    "feasibility": "brief feasibility assessment",
    "inspired_by": ["paper or method title"]
  }}
]
"""
        result = worker.json_or_error(prompt)
        data = result.get("data")
        candidates = data if result.get("ok") and isinstance(data, list) else []
        if not candidates:
            log(f"Idea worker {worker_index} unavailable: {str(result.get('error') or 'no JSON candidates returned')[:300]}")
        return [item for item in candidates if isinstance(item, dict)], worker.summary()

    candidate_groups: list[list[dict]] = [[] for _ in perspectives]
    summaries: list[dict] = [{} for _ in perspectives]
    with ThreadPoolExecutor(max_workers=len(perspectives)) as executor:
        futures = {
            executor.submit(run_worker, index, perspective): index
            for index, perspective in enumerate(perspectives, 1)
        }
        for future in as_completed(futures):
            index = futures[future] - 1
            candidate_groups[index], summaries[index] = future.result()
            for candidate in candidate_groups[index]:
                candidate["worker_perspective"] = perspectives[index]
    return [candidate for group in candidate_groups for candidate in group], summaries


def _synthesize_worker_candidates(
    main_agent: LLMClient,
    max_candidates: int,
    profile: dict,
    method_evaluation: dict,
    worker_candidates: list[dict],
    broad_signals: list[dict],
    log: LogFn,
) -> tuple[list[dict], list[dict]]:
    prompt = f"""
Continue as the main research agent.

Using your existing auto_read context, method evaluation, and worker candidate ideas, produce a diverse candidate pool for later preliminary experimental planning.

Normalized researcher profile:
{json.dumps(profile, ensure_ascii=False, indent=2)}

Method evaluation:
{json.dumps(method_evaluation, ensure_ascii=False, indent=2)}

Worker candidates:
{json.dumps(worker_candidates, ensure_ascii=False, indent=2)}

Broad discovery signals:
{json.dumps(broad_signals, ensure_ascii=False, indent=2)}

Review, merge, reject, and improve worker candidates.
Do not create detailed experimental plans yet.

Return one strict JSON object:
{{
  "candidate_ideas": [
    {{
      "title": "Chinese title",
      "hypothesis": "testable Chinese hypothesis",
      "motivation": "evidence-backed motivation",
      "novelty": "specific novelty",
      "feasibility": "brief feasibility assessment",
      "inspired_by": ["paper or method title"]
    }}
  ],
  "rejected_candidates": [
    {{
      "title": "candidate title",
      "reason": "rejection reason"
    }}
  ]
}}
Return at most {max_candidates} candidate ideas.
"""
    result = main_agent.json_or_error(prompt)
    data = result.get("data")
    if result.get("ok") and isinstance(data, dict):
        candidates = data.get("candidate_ideas", [])
        rejected = data.get("rejected_candidates", [])
        log(f"Main agent synthesized {len(candidates) if isinstance(candidates, list) else 0} idea candidates")
        return (
            [item for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else [],
            [item for item in rejected if isinstance(item, dict)] if isinstance(rejected, list) else [],
        )
    log(f"Main agent idea synthesis unavailable: {str(result.get('error') or '')[:300]}")
    return [], []


def run_idea(request: IdeaRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    idea_dir = stage_dir(directory, "idea")
    _raise_if_cancelled(should_cancel)
    items = _source_items(directory)
    max_ideas = request.max_ideas or config.max_ideas
    agent_conversation = f"run:{request.run_id}:main"
    generator = LLMClient(config, "idea_generator", conversation_key=agent_conversation, resume_session=True)
    judge = LLMClient(config, "idea_judge", conversation_key=agent_conversation, resume_session=True)
    workers = clamp_workers(request.parallel_workers or config.idea_parallel_workers, default=1, maximum=8)
    candidate_multiplier = max(1, int(request.candidate_multiplier or 2))
    ideas = _fallback_ideas(items, max_ideas)
    candidate_pool: list[dict] = []
    judge_scores: list[dict] = []
    method_evaluation: dict = {}
    worker_candidates: list[dict] = []
    rejected_candidates: list[dict] = []
    worker_summaries: list[dict] = []

    if generator.enabled and items:
        _raise_if_cancelled(should_cancel)
        method_evaluation = _evaluate_methods(directory, generator, log)
        _raise_if_cancelled(should_cancel)
        profile_result = read_json(existing_stage_path(directory, "find", "stage0_profile.json"), {})
        profile = profile_result.get("profile", {}) if isinstance(profile_result, dict) else {}
        read_results = read_json(existing_stage_path(directory, "read", "read_results.json"), {})
        content_only = [
            reading.get("content", {})
            for reading in read_results.get("readings", [])
            if isinstance(reading, dict) and isinstance(reading.get("content"), dict)
        ]
        evidence = {
            "main_agent_summary": read_results.get("main_agent_summary") or read_results.get("cross_summary", {}),
            "method_analysis": read_results.get("method_analysis", {}),
            "readings": content_only,
        }
        active_workers = min(workers, 5)
        worker_candidates, worker_summaries = _generate_worker_candidates(
            request.run_id,
            config,
            active_workers,
            candidate_multiplier * max_ideas,
            profile,
            method_evaluation,
            evidence,
            log,
        )
        _raise_if_cancelled(should_cancel)
        synthesized, rejected_candidates = _synthesize_worker_candidates(
            generator,
            candidate_multiplier * max_ideas,
            profile,
            method_evaluation,
            worker_candidates,
            [item for item in items if item.get("source") not in {"read", "main_agent_summary", "method_analysis"}],
            log,
        )
        for index, item in enumerate(synthesized or worker_candidates, 1):
            item.setdefault("id", f"idea-candidate-{index:03d}")
            item.setdefault("status", "pending")
            item.setdefault("inspired_by", [])
            item["inspired_by"] = [
                source if isinstance(source, dict) else {"title": str(source), "source": "read", "url": ""}
                for source in item["inspired_by"]
            ]
            item.setdefault("min_experiment", "")
            item.setdefault("score", 0)
            candidate_pool.append(item)

        finalist_pool = _dedupe_ideas(candidate_pool)
        if finalist_pool:
            ideas = sorted(finalist_pool, key=_score_value, reverse=True)[:max_ideas]
        else:
            log("No agent-generated idea candidates accepted; preserving fallback ideas.")
        if judge.enabled and finalist_pool:
            _raise_if_cancelled(should_cancel)
            judge_items = "\n".join(f"- {item.get('id')}: {item.get('title')} | score={item.get('score')} | hypothesis={item.get('hypothesis')}" for item in finalist_pool)
            judge_prompt = f"""
You are the final judge for research ideas. Select the best {max_ideas} ideas for the researcher.

Research interest:
{config.research_interest}

Researcher profile:
{config.researcher_profile}

Candidate ideas:
{judge_items}

Return strict JSON:
{{"selected":[{{"id":"candidate id","judge_score":0-10,"judge_reason":"Chinese reason"}}]}}
"""
            judged = judge.json_or_none(judge_prompt)
            selected_rows = judged.get("selected", []) if isinstance(judged, dict) else []
            by_id = {item.get("id", ""): item for item in finalist_pool}
            selected: list[dict] = []
            for row in selected_rows:
                if not isinstance(row, dict):
                    continue
                item = by_id.get(str(row.get("id") or ""))
                if not item:
                    continue
                item["judge_score"] = float(row.get("judge_score") or item.get("score") or 0)
                item["judge_reason"] = str(row.get("judge_reason") or "")
                selected.append(item)
                judge_scores.append({"id": item.get("id"), "judge_score": item["judge_score"], "judge_reason": item["judge_reason"]})
            if selected:
                ideas = sorted(_dedupe_ideas(selected), key=lambda item: float(item.get("judge_score") or 0), reverse=True)[:max_ideas]
            else:
                log("Idea judge returned no usable selection; preserving ranked generated candidates.")

        for index, idea in enumerate(ideas, 1):
            idea["id"] = f"idea-{index:03d}"
            idea["status"] = "pending"
        log(f"Generated {len(candidate_pool)} candidates, selected {len(ideas)} ideas")

    _raise_if_cancelled(should_cancel)
    write_json(idea_dir / "ideas.json", {
        "run_id": request.run_id,
        "ideas": ideas,
        "candidate_pool": candidate_pool,
        "judge_scores": judge_scores,
        "method_evaluation": method_evaluation,
        "worker_candidates": worker_candidates,
        "rejected_candidates": rejected_candidates,
        "llm": {"generator": generator.summary(), "judge": judge.summary(), "workers": workers, "idea_workers": worker_summaries},
    })
    write_text(idea_dir / "idea.md", render_ideas_markdown(ideas))
    sync_latest("auto_idea", "idea.md", idea_dir / "idea.md")
    update_manifest(directory, "idea")
    return {"run_id": request.run_id, "ideas": ideas, "method_evaluation": method_evaluation}


def render_ideas_markdown(ideas: list[dict]) -> str:
    lines = ["# Research Ideas", ""]
    for index, idea in enumerate(ideas, 1):
        lines.extend([
            f"## {index}. {idea.get('title', 'Untitled')}",
            "",
            f"- **ID**: `{idea.get('id', '')}`",
            f"- **Status**: {idea.get('status', 'pending')}",
            f"- **Novelty**: {idea.get('novelty', '')}",
            f"- **Feasibility**: {idea.get('feasibility', '')}",
            f"- **Score**: {idea.get('score', '')}",
            "",
            "### Hypothesis",
            idea.get("hypothesis", ""),
            "",
            "### Minimum Experiment",
            idea.get("min_experiment", ""),
            "",
            "### Inspired By",
        ])
        for source in idea.get("inspired_by", []):
            lines.append(f"- [{source.get('source', '')}] [{source.get('title', '')}]({source.get('url', '')})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def patch_idea(run_id: str, idea_id: str, patch: IdeaPatch) -> dict:
    directory = run_dir(run_id)
    idea_dir = stage_dir(directory, "idea")
    data = read_json(existing_stage_path(directory, "idea", "ideas.json"), {"run_id": run_id, "ideas": []})
    for idea in data.get("ideas", []):
        if idea.get("id") == idea_id:
            updates = patch.model_dump(exclude_none=True)
            idea.update(updates)
            break
    write_json(idea_dir / "ideas.json", data)
    write_text(idea_dir / "idea.md", render_ideas_markdown(data.get("ideas", [])))
    sync_latest("auto_idea", "idea.md", idea_dir / "idea.md")
    return data


def confirm_idea(run_id: str, idea_id: str) -> dict:
    directory = run_dir(run_id)
    idea_dir = stage_dir(directory, "idea")
    data = read_json(existing_stage_path(directory, "idea", "ideas.json"), {"run_id": run_id, "ideas": []})
    selected = next((idea for idea in data.get("ideas", []) if idea.get("id") == idea_id), None)
    if not selected:
        raise ValueError(f"Idea not found: {idea_id}")
    for idea in data.get("ideas", []):
        idea["status"] = "approved" if idea.get("id") == idea_id else "deleted"
    data["selected_idea_id"] = idea_id
    write_json(idea_dir / "ideas.json", data)
    write_text(idea_dir / "idea.md", render_ideas_markdown(data.get("ideas", [])))
    sync_latest("auto_idea", "idea.md", idea_dir / "idea.md")
    return data
