from __future__ import annotations

from typing import Callable

from auto_research.jobs import JobCancelled
from auto_research.llm import LLMClient, clamp_workers, parallel_json
from auto_research.models import AppConfig, IdeaPatch, IdeaRequest
from auto_research.storage import read_json, run_dir, sync_latest, update_manifest, write_json, write_text


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _source_items(directory) -> list[dict]:
    find_results = read_json(directory / "find_results.json", {})
    read_results = read_json(directory / "read_results.json", {})
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
        items.append({
            "source": "read",
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "summary": item.get("summary", ""),
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


def _item_windows(items: list[dict], workers: int) -> list[list[dict]]:
    if not items:
        return []
    active = min(max(1, workers), len(items))
    size = max(1, (len(items) + active - 1) // active)
    return [items[index:index + size] for index in range(0, len(items), size)][:active]


def run_idea(request: IdeaRequest, config: AppConfig, log: LogFn = print, should_cancel: CancelFn = lambda: False) -> dict:
    directory = run_dir(request.run_id)
    _raise_if_cancelled(should_cancel)
    items = _source_items(directory)
    max_ideas = request.max_ideas or config.max_ideas
    generator = LLMClient(config, "idea_generator")
    judge = LLMClient(config, "idea_judge")
    workers = clamp_workers(request.parallel_workers or config.idea_parallel_workers, default=1, maximum=8)
    candidate_multiplier = max(1, int(request.candidate_multiplier or 2))
    ideas = _fallback_ideas(items, max_ideas)
    candidate_pool: list[dict] = []
    judge_scores: list[dict] = []

    if generator.enabled and items:
        _raise_if_cancelled(should_cancel)
        windows = _item_windows(items, workers)
        prompts: list[str] = []
        for batch_index, window in enumerate(windows, 1):
            prompt_items = "\n".join(f"- [{item['source']}] {item['title']} :: {item['summary'][:500]} URL={item['url']}" for item in window)
            prompts.append(f"""
Generate {candidate_multiplier * max_ideas} research ideas for this researcher.

Research interest:
{config.research_interest}

Researcher profile:
{config.researcher_profile}

Signals:
{prompt_items}

Return strict JSON array. Each item:
{{"id":"idea-001","title":"Chinese title","hypothesis":"Chinese hypothesis","min_experiment":"Chinese min experiment","novelty":"HIGH/MEDIUM/LOW","feasibility":"HIGH/MEDIUM/LOW","score":8.5,"inspired_by":[{{"title":"","source":"","url":""}}]}}

Batch index: {batch_index}. Use only the signals shown in this batch.
"""
)
        generator_results = parallel_json(generator, prompts, workers)
        finalist_pool: list[dict] = []
        for batch_index, result in enumerate(generator_results, 1):
            data = result.get("data")
            batch_candidates = data if isinstance(data, list) else []
            normalized: list[dict] = []
            for index, item in enumerate(batch_candidates, 1):
                if not isinstance(item, dict):
                    continue
                item.setdefault("id", f"idea-b{batch_index}-{index:03d}")
                item.setdefault("status", "pending")
                item.setdefault("inspired_by", [])
                item["generator_batch"] = batch_index
                normalized.append(item)
            candidate_pool.extend(normalized)
            finalist_pool.extend(sorted(normalized, key=_score_value, reverse=True)[:2])

        finalist_pool = _dedupe_ideas(finalist_pool)
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
            ideas = sorted(_dedupe_ideas(finalist_pool), key=_score_value, reverse=True)[:max_ideas]

        for index, idea in enumerate(ideas, 1):
            idea["id"] = f"idea-{index:03d}"
            idea["status"] = "pending"
        log(f"Generated {len(candidate_pool)} candidates, selected {len(ideas)} ideas")

    _raise_if_cancelled(should_cancel)
    write_json(directory / "ideas.json", {
        "run_id": request.run_id,
        "ideas": ideas,
        "candidate_pool": candidate_pool,
        "judge_scores": judge_scores,
        "llm": {"generator": generator.summary(), "judge": judge.summary(), "workers": workers},
    })
    write_text(directory / "idea.md", render_ideas_markdown(ideas))
    sync_latest("auto_idea", "idea.md", directory / "idea.md")
    update_manifest(directory, "idea")
    return {"run_id": request.run_id, "ideas": ideas}


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
    data = read_json(directory / "ideas.json", {"run_id": run_id, "ideas": []})
    for idea in data.get("ideas", []):
        if idea.get("id") == idea_id:
            updates = patch.model_dump(exclude_none=True)
            idea.update(updates)
            break
    write_json(directory / "ideas.json", data)
    write_text(directory / "idea.md", render_ideas_markdown(data.get("ideas", [])))
    sync_latest("auto_idea", "idea.md", directory / "idea.md")
    return data
