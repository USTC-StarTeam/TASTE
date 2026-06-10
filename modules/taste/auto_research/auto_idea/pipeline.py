from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from auto_research.jobs import JobCancelled
from auto_research.paths import ROOT
from auto_research.llm import LLMClient, clamp_workers, extract_partial_json_array, parallel_json
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
    for source_name in ("articles", "huggingface", "github"):
        for item in find_results.get(source_name, []):
            summary = item.get("reason") or item.get("fit_explanation") or item.get("abstract", "")
            items.append({
                "source": source_name,
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "summary": str(summary)[:700],
                "score": item.get("score", ""),
                "fit_score": item.get("fit_score", ""),
                "hit_directions": item.get("hit_directions", []),
            })
    for item in read_results.get("readings", []):
        items.append({
            "source": "read",
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "summary": str(item.get("summary", ""))[:700],
        })
    return items


def _item_text(item: dict) -> str:
    return f"{item.get('title', '')} {item.get('summary', '')}".lower()


def _config_topic_text(config: AppConfig | None) -> str:
    if config is None:
        return ""
    parts = [config.research_interest, config.researcher_profile]
    selection = config.default_find_selection if isinstance(config.default_find_selection, dict) else {}
    for key in ("topic", "research_interest", "user_prompt"):
        value = selection.get(key)
        if value:
            parts.append(str(value))
    return " ".join(part for part in parts if part).strip()


def _topic_terms(config: AppConfig | None) -> list[str]:
    import re
    text = _config_topic_text(config).lower()
    terms = []
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", text):
        if token in {"research", "paper", "model", "method", "data", "dataset", "system", "experiment", "baseline"}:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:40]


def _topic_rank(item: dict, config: AppConfig | None = None) -> tuple[int, float]:
    text = _item_text(item)
    terms = _topic_terms(config)
    topic_hits = sum(1 for term in terms if term.lower() in text)
    try:
        score = float(item.get("score") or item.get("fit_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return min(topic_hits, 10), score


def _select_relevant_seeds(items: list[dict], limit: int, config: AppConfig | None = None) -> list[dict]:
    ranked = sorted(items, key=lambda item: _topic_rank(item, config), reverse=True)
    selected = [item for item in ranked if _topic_rank(item, config)[0] > 0]
    if len(selected) < limit:
        selected.extend(item for item in ranked if item not in selected)
    return selected[:limit]


GENERIC_IDEA_EXPERIMENT_MARKERS = (
    "after environment-stage base selection",
    "after environment review",
    "run a minimal same-protocol baseline/candidate/ablation",
    "baseline/candidate/ablation experiment with audited metrics and bad cases",
)


def _is_generic_experiment_placeholder(value: object) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and any(marker in text for marker in GENERIC_IDEA_EXPERIMENT_MARKERS))


def _normalize_inspired_by(value: object, fallback: list[dict] | None = None) -> list[dict]:
    rows: list[dict] = []
    source = value if isinstance(value, list) else []
    for item in source:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("paper_title") or item.get("name") or "").strip()
            if not title:
                continue
            rows.append({
                "title": title,
                "source": str(item.get("source") or item.get("venue") or item.get("role") or "").strip(),
                "url": str(item.get("url") or item.get("pdf_url") or "").strip(),
                "reason": str(item.get("reason") or item.get("mechanism") or item.get("use") or "").strip(),
            })
        elif str(item or "").strip():
            rows.append({"title": str(item).strip(), "source": "", "url": "", "reason": ""})
    if not rows and fallback:
        rows = _normalize_inspired_by(fallback, None)
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = (row.get("title", "") + "|" + row.get("url", "")).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out[:8]


def _inspired_by_text(value: object) -> str:
    rows = _normalize_inspired_by(value)
    lines = []
    for row in rows:
        parts = [row.get("title", "")]
        meta = " / ".join(part for part in [row.get("source", ""), row.get("reason", "")] if part)
        if meta:
            parts.append(meta)
        if row.get("url"):
            parts.append(row["url"])
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _parse_inspired_by_text(value: object) -> list[dict]:
    rows: list[dict] = []
    for line in str(value or "").splitlines():
        text = line.strip().strip("-").strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split("|")]
        title = parts[0] if parts else text
        source = parts[1] if len(parts) > 1 else ""
        url = next((part for part in parts[1:] if part.startswith(("http://", "https://"))), "")
        reason = " / ".join(part for part in parts[1:] if part and part != source and part != url)
        rows.append({"title": title, "source": source, "url": url, "reason": reason})
    return _normalize_inspired_by(rows)


def _normalize_idea_schema(idea: dict, fallback_refs: list[dict] | None = None) -> dict:
    if not isinstance(idea, dict):
        return idea
    new_method = str(idea.get("new_method") or idea.get("hypothesis") or "").strip()
    method_details = str(idea.get("method_details") or idea.get("mechanism") or idea.get("rationale") or "").strip()
    initial_experiment = str(
        idea.get("initial_experiment")
        or idea.get("experiment_design")
        or idea.get("experimental_design")
        or idea.get("min_experiment")
        or idea.get("minimum_experiment")
        or ""
    ).strip()
    if _is_generic_experiment_placeholder(initial_experiment):
        initial_experiment = ""
        idea["initial_experiment_required"] = True
        idea["initial_experiment_needs_project_agent"] = True
        for key in ("initial_experiment", "min_experiment", "minimum_experiment", "experiment_design", "experimental_design"):
            idea.pop(key, None)
    if new_method:
        idea["new_method"] = new_method
        idea.setdefault("hypothesis", new_method)
    if method_details:
        idea["method_details"] = method_details
        idea.setdefault("mechanism", method_details)
    if initial_experiment:
        idea["initial_experiment"] = initial_experiment
        idea["min_experiment"] = initial_experiment
        idea["minimum_experiment"] = initial_experiment
        idea["initial_experiment_required"] = False
        idea.pop("initial_experiment_needs_project_agent", None)
    else:
        idea["initial_experiment_required"] = True
        idea["initial_experiment_needs_project_agent"] = True
        for key in ("initial_experiment", "min_experiment", "minimum_experiment"):
            if not str(idea.get(key) or "").strip():
                idea.pop(key, None)
    inspired = _normalize_inspired_by(idea.get("inspired_by"), fallback_refs)
    if inspired:
        idea["inspired_by"] = inspired
        idea["inspired_by_text"] = _inspired_by_text(inspired)
    return idea


def _fallback_ideas(items: list[dict], max_ideas: int, config: AppConfig | None = None) -> list[dict]:
    seeds = _select_relevant_seeds(items, max(3, max_ideas * 3), config)
    source_refs = [{"title": item.get("title", ""), "source": item.get("source", ""), "url": item.get("url", "")} for item in seeds[:4]]
    titles = "；".join(ref["title"] for ref in source_refs if ref.get("title"))
    topic = _config_topic_text(config) or "当前项目主题"
    templates = [
        {
            "title": "当前主题的最小可复现实验闭环",
            "hypothesis": f"围绕“{topic[:80]}”的下一步科研应先建立一个最小、可复现、可审计的实验闭环；只有真实数据、固定协议、完整日志和反例切片同时落盘后，结果才允许进入论文证据。",
            "mechanism": "从当前高证据文献和可运行代码中抽取一个最小机制改动，保持数据、训练预算、评测指标和对照协议一致，并把所有配置、日志、指标、坏例和反例写入本地产物。",
            "repo_or_data_path": "由项目 Claude Code 在当前项目 workspace 内选择已通过 repo/data/protocol 审计的路径；缺失任一证据时保持 blocked，不用占位路径。",
            "initial_experiment": "",
            "initial_experiment_required": True,
            "bad_case_slice": "至少覆盖整体失败、关键用户/样本切片、对照退化、数据缺失或协议不一致四类反例。",
            "novelty": "MEDIUM",
            "feasibility": "HIGH",
            "evidence_strength": "MEDIUM",
            "score": 8.2,
        },
        {
            "title": "失败切片驱动的机制修复实验",
            "hypothesis": "如果上一轮候选实验在某些切片上系统性失败，针对这些切片提出最小机制修复并做同协议反例压力测试，比继续扩大训练预算更能判断路线是否值得保留。",
            "mechanism": "先读取最近实验日志、审计记录和坏例文件，定位失败切片；再只修改一个机制因素，比较修复前后整体指标、切片指标和反例退化。",
            "repo_or_data_path": "复用当前已审计路线的代码、数据和日志；若缺少坏例文件，先补写 bad_cases/counterexamples 后再设计训练。",
            "initial_experiment": "",
            "initial_experiment_required": True,
            "bad_case_slice": "上一轮最差切片、对照退化切片、数据稀疏切片、机制假设不适用切片。",
            "novelty": "MEDIUM",
            "feasibility": "MEDIUM",
            "evidence_strength": "MEDIUM",
            "score": 7.9,
        },
        {
            "title": "证据对齐的可审计增强模块",
            "hypothesis": "只有当增强模块的输入证据、机制作用点和目标指标三者一致时，候选方法才可能支撑当前论文主张；否则应降级为负结果或路线剪枝记录。",
            "mechanism": "把文献机制、项目数据字段、模型接口和评测指标逐项对齐，缺失项由项目 Claude Code 写明不可推广原因；通过后再实现最小增强模块。",
            "repo_or_data_path": "选择当前项目中可追溯的 repo/data/artifact；所有新增字段必须写入 experiment.json、metrics.json、audit.json 和 bad_cases.json。",
            "initial_experiment": "",
            "initial_experiment_required": True,
            "bad_case_slice": "关键输入缺失、机制输入与目标指标不一致、增强模块关闭后仍提升、替换切片后退化。",
            "novelty": "MEDIUM",
            "feasibility": "MEDIUM",
            "evidence_strength": "LOW",
            "score": 7.5,
        },
    ]
    ideas = []
    for index, item in enumerate(templates[:max_ideas], 1):
        idea = dict(item)
        idea.update({
            "id": f"idea-{index:03d}",
            "status": "pending",
            "new_method": item.get("new_method") or item.get("hypothesis", ""),
            "method_details": item.get("method_details") or item.get("mechanism", ""),
            "initial_experiment": item.get("initial_experiment") or item.get("min_experiment", ""),
            "inspired_by": source_refs[:3] if source_refs else [],
            "fallback_reason": f"LLM idea generation was unavailable or unparsable; generated project-config-driven fallback from retrieved signals: {titles[:300]}",
        })
        ideas.append(_normalize_idea_schema(idea, source_refs[:3] if source_refs else []))
    return ideas


def _quality_flags(idea: dict, config: AppConfig | None = None) -> list[str]:
    flags: list[str] = []
    required = ["title", "hypothesis", "mechanism", "repo_or_data_path", "min_experiment", "bad_case_slice"]
    for key in required:
        if len(str(idea.get(key, "")).strip()) < 20:
            flags.append(f"missing_or_short:{key}")
    text = " ".join(str(idea.get(key, "")) for key in required).lower()
    if "placeholder" in text or "github.com/example" in text:
        flags.append("placeholder_evidence")
    terms = _topic_terms(config)
    if terms and not any(term.lower() in text for term in terms):
        flags.append("missing_project_topic_alignment")
    return flags


def _usable_idea(idea: dict, min_judge_score: float = 6.0, config: AppConfig | None = None) -> bool:
    try:
        judge_score = float(idea.get("judge_score", idea.get("score", 0)) or 0)
    except (TypeError, ValueError):
        judge_score = 0.0
    return judge_score >= min_judge_score and not _quality_flags(idea, config)


def _fill_with_fallback(selected: list[dict], items: list[dict], max_ideas: int, config: AppConfig | None = None) -> list[dict]:
    filled = list(selected)
    seen = {str(item.get("title", "")).lower() for item in filled}
    for fallback in _fallback_ideas(items, max_ideas, config):
        key = str(fallback.get("title", "")).lower()
        if key in seen:
            continue
        fallback["status"] = "pending"
        fallback["quality_gate_note"] = "Filled by evidence-guided fallback because LLM candidates were incomplete, low-score, or used placeholder evidence."
        filled.append(fallback)
        seen.add(key)
        if len(filled) >= max_ideas:
            break
    return filled[:max_ideas]


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


def _display_score_value(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "n/a", "na"}:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    return f"{number:.2f}".rstrip("0").rstrip(".") or "0"


def _idea_markdown_score(idea: dict) -> str:
    objective_scores = idea.get("objective_scores") if isinstance(idea.get("objective_scores"), dict) else {}
    for value in [
        objective_scores.get("overall"),
        objective_scores.get("overall_score"),
        idea.get("score"),
        idea.get("idea_score"),
        idea.get("judge_score"),
    ]:
        display = _display_score_value(value)
        if display is not None:
            return display
    return "未评分"


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
    if generator.enabled:
        if hasattr(generator, "timeout_sec"):
            generator.timeout_sec = max(generator.timeout_sec, int(__import__("os").environ.get("IDEA_TIMEOUT_SEC", "300")))
        if hasattr(generator, "max_tokens"):
            generator.max_tokens = max(generator.max_tokens, int(__import__("os").environ.get("IDEA_MAX_TOKENS", "5000")))
    if judge.enabled:
        if hasattr(judge, "timeout_sec"):
            judge.timeout_sec = max(judge.timeout_sec, int(__import__("os").environ.get("IDEA_TIMEOUT_SEC", "300")))
        if hasattr(judge, "max_tokens"):
            judge.max_tokens = max(judge.max_tokens, int(__import__("os").environ.get("IDEA_MAX_TOKENS", "3000")))
    workers = clamp_workers(request.parallel_workers or config.idea_parallel_workers, default=1, maximum=8)
    candidate_multiplier = max(1, int(request.candidate_multiplier or 2))
    ideas = _fallback_ideas(items, max_ideas, config)
    candidate_pool: list[dict] = []
    judge_scores: list[dict] = []
    llm_errors: list[dict] = []
    strict_quality_gate = bool((config.research_interest or "").strip())

    use_llm_idea = os.environ.get("USE_LLM_IDEA", "1").lower() in {"1", "true", "yes", "on"}
    if generator.enabled and items and use_llm_idea:
        _raise_if_cancelled(should_cancel)
        windows = _item_windows(items, workers)
        prompts: list[str] = []
        for batch_index, window in enumerate(windows, 1):
            prompt_items = "\n".join(
                f"- [{item['source']}] title={item['title'][:160]} | score={item.get('score','')} | summary={item['summary'][:360]} | url={item['url']}"
                for item in window[:8]
            )
            prompts.append(f"""
Generate exactly ONE runnable research idea as JSON array with one object.
Research interest:
{config.research_interest or config.researcher_profile}
Evidence:
{prompt_items}
Return only JSON, no reasoning, no markdown. The idea must be aligned to the research interest and the evidence, and must not invent a project-specific method or dataset that is absent from the evidence. Each idea must contain: (1) new_method: a detailed proposed method, (2) initial_experiment: which prior work/base it starts from, what exact change is tested, baseline/control/ablation, metrics, and bad-case slice, (3) inspired_by: the papers/items that inspired the method and why.
[{{"id":"idea-001","title":"中文标题","new_method":"详细的新方法；说明核心假设、机制、模块和为什么可能有效","method_details":"方法机制细节；说明输入、模型改动、训练/推理作用点","initial_experiment":"初步详细实验；说明基于哪篇工作或哪个可审计基底，做什么最小改动，对比哪些 baseline/control/ablation，使用哪些指标和坏例切片","hypothesis":"可选旧字段，等同 new_method 的短摘要","mechanism":"可选旧字段，等同 method_details","repo_or_data_path":"只写证据状态或缺口；不得臆造路径","bad_case_slice":"坏例切片","novelty":"HIGH/MEDIUM/LOW","feasibility":"HIGH/MEDIUM/LOW","evidence_strength":"HIGH/MEDIUM/LOW","score":8.5,"inspired_by":[{{"title":"来源标题","source":"articles/read/github","url":"url","reason":"启发了哪个机制或实验对照"}}]}}]
"""
)
        generator_results = parallel_json(generator, prompts, workers)
        finalist_pool: list[dict] = []
        for batch_index, result in enumerate(generator_results, 1):
            data = result.get("data")
            if not result.get("ok"):
                llm_errors.append({"stage": "idea_generator", "batch": batch_index, "error": str(result.get("error", ""))[:1000], "raw_text": str(result.get("raw_text", ""))[:2000]})
            batch_candidates = []
            if isinstance(data, list):
                batch_candidates = data
            elif isinstance(data, dict):
                idea_like_keys = {"title", "hypothesis", "mechanism", "min_experiment", "new_method", "method_details", "initial_experiment", "inspired_by"}
                if idea_like_keys.intersection(data.keys()):
                    batch_candidates = [data]
                else:
                    for key in ("ideas", "candidates", "research_ideas", "items", "output"):
                        value = data.get(key)
                        if isinstance(value, list):
                            batch_candidates = value
                            break
                if not batch_candidates:
                    llm_errors.append({"stage": "idea_generator", "batch": batch_index, "error": "dict response lacked expected idea keys", "raw_text": str(result.get("raw_text", ""))[:2000], "keys": list(data.keys())[:20]})
            if not batch_candidates and isinstance(result.get("raw_text"), str):
                raw_text = result.get("raw_text", "")
                for key in ("ideas", "candidates"):
                    if f'"{key}"' in raw_text:
                        try:
                            from auto_research.llm import extract_json
                            parsed = extract_json(raw_text)
                            if isinstance(parsed, dict):
                                batch_candidates = parsed.get("ideas") or parsed.get("candidates") or []
                            elif isinstance(parsed, list):
                                batch_candidates = parsed
                            break
                        except Exception as exc:
                            llm_errors.append({"stage": "idea_generator", "batch": batch_index, "error": f"fallback parse failed: {exc}"[:1000]})
                if not batch_candidates:
                    recovered = extract_partial_json_array(raw_text)
                    if recovered:
                        batch_candidates = recovered
                        llm_errors.append({"stage": "idea_generator", "batch": batch_index, "warning": f"Recovered {len(recovered)} complete ideas from truncated JSON array."})
            normalized: list[dict] = []
            for index, item in enumerate(batch_candidates, 1):
                if not isinstance(item, dict):
                    continue
                item.setdefault("id", f"idea-b{batch_index}-{index:03d}")
                item.setdefault("status", "pending")
                item.setdefault("inspired_by", [])
                item["generator_batch"] = batch_index
                normalized.append(_normalize_idea_schema(item))
            candidate_pool.extend(normalized)
            finalist_pool.extend(sorted(normalized, key=_score_value, reverse=True)[:2])

        finalist_pool = _dedupe_ideas(finalist_pool)
        if not finalist_pool:
            llm_errors.append({"stage": "idea_generator", "batch": "all", "error": "LLM produced no parseable idea candidates; preserved signal-based fallback ideas."})
            ideas = _fallback_ideas(items, max_ideas, config)
        elif judge.enabled and finalist_pool:
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
            if not isinstance(judged, dict):
                llm_errors.append({"stage": "idea_judge", "batch": "final", "error": "Judge returned no parseable JSON; using generator ranking."})
            selected_rows = []
            if isinstance(judged, dict):
                for key in ("selected", "ideas", "candidates", "outputs"):
                    value = judged.get(key)
                    if isinstance(value, list):
                        selected_rows = value
                        break
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
                ranked_selected = sorted(_dedupe_ideas(selected), key=lambda item: float(item.get("judge_score") or 0), reverse=True)
                usable = [item for item in ranked_selected if (not strict_quality_gate or _usable_idea(item, config=config))]
                rejected = [item for item in selected if strict_quality_gate and not _usable_idea(item)]
                for item in rejected:
                    llm_errors.append({"stage": "idea_quality_gate", "id": item.get("id", ""), "flags": _quality_flags(item, config), "judge_score": item.get("judge_score")})
                ideas = _fill_with_fallback(usable, items, max_ideas, config)
            else:
                ranked_finalists = sorted(_dedupe_ideas(finalist_pool), key=_score_value, reverse=True)
                usable = [item for item in ranked_finalists if (not strict_quality_gate or _usable_idea(item, min_judge_score=5.5, config=config))]
                for item in finalist_pool:
                    if strict_quality_gate and not _usable_idea(item, min_judge_score=5.5):
                        llm_errors.append({"stage": "idea_quality_gate", "id": item.get("id", ""), "flags": _quality_flags(item, config), "score": item.get("score")})
                ideas = _fill_with_fallback(usable, items, max_ideas, config)
        else:
            ranked_finalists = sorted(_dedupe_ideas(finalist_pool), key=_score_value, reverse=True)
            usable = [item for item in ranked_finalists if (not strict_quality_gate or _usable_idea(item, min_judge_score=5.5, config=config))]
            for item in finalist_pool:
                if strict_quality_gate and not _usable_idea(item, min_judge_score=5.5):
                    llm_errors.append({"stage": "idea_quality_gate", "id": item.get("id", ""), "flags": _quality_flags(item, config), "score": item.get("score")})
            ideas = _fill_with_fallback(usable, items, max_ideas, config)

        for index, idea in enumerate(ideas, 1):
            idea["id"] = f"idea-{index:03d}"
            idea["status"] = "pending"
            _normalize_idea_schema(idea)
        fallback_count = sum(1 for idea in ideas if str(idea.get("id", "")).startswith("fallback-"))
        fallback_note = f"; fallback-filled {fallback_count}" if fallback_count else ""
        log(f"Idea generation produced {len(candidate_pool)} LLM candidates; selected {len(ideas)} ideas{fallback_note}")

    _raise_if_cancelled(should_cancel)
    ideas = [_normalize_idea_schema(idea) for idea in ideas]
    idea_payload = {
        "run_id": request.run_id,
        "ideas": ideas,
        "candidate_pool": candidate_pool,
        "judge_scores": judge_scores,
        "llm": {"generator": generator.summary(), "judge": judge.summary(), "workers": workers, "errors": llm_errors},
    }
    idea_markdown = render_ideas_markdown(ideas)
    write_json(directory / "ideas.json", idea_payload)
    write_text(directory / "idea.md", idea_markdown)
    sync_latest("auto_idea", "ideas.json", directory / "ideas.json")
    sync_latest("auto_idea", "idea.md", directory / "idea.md")
    _sync_project_ideas(request.run_id, idea_payload, idea_markdown)
    update_manifest(directory, "idea")
    return {"run_id": request.run_id, "ideas": ideas}


def _markdown_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(f"- {_markdown_value(item)}" for item in value)
    if isinstance(value, dict):
        return "\n".join(f"- **{key}**: {_markdown_value(item)}" for key, item in value.items())
    return str(value)


def _idea_public_new_method_text(idea: dict) -> str:
    base = str(idea.get("new_method") or idea.get("hypothesis") or "").strip()
    details = str(idea.get("method_details") or idea.get("mechanism") or "").strip()
    if base and details and details not in base:
        return f"{base}\n\n{details}"
    return base or details


def _idea_public_initial_experiment_text(idea: dict) -> str:
    value = str(
        idea.get("initial_experiment")
        or idea.get("experiment_design")
        or idea.get("experimental_design")
        or idea.get("min_experiment")
        or idea.get("minimum_experiment")
        or ""
    ).strip()
    if _is_generic_experiment_placeholder(value):
        return ""
    return value


def render_ideas_markdown(ideas: list[dict]) -> str:
    lines = ["# 当前 Find 驱动 Ideas", ""]
    for index, idea in enumerate(ideas, 1):
        inspired = _normalize_inspired_by(idea.get("inspired_by"), idea.get("supporting_papers") or idea.get("positive_anchor_papers"))
        lines.extend([
            f"## {index}. {_markdown_value(idea.get('title') or 'Untitled')}",
            "",
            f"- id: `{_markdown_value(idea.get('id') or idea.get('idea_id') or '')}`",
            f"- status: {_markdown_value(idea.get('status', 'pending'))}",
            f"- score: {_idea_markdown_score(idea)}",
            "",
            "### 新方法",
            _markdown_value(_idea_public_new_method_text(idea)),
            "",
            "### 初步实验",
            _markdown_value(_idea_public_initial_experiment_text(idea) or "待项目代理根据精读结果补齐：需要说明基于哪项工作或基底、做什么最小改动、对比哪些 baseline/control/ablation、使用哪些指标和坏例切片。"),
            "",
            "### Inspired by",
        ])
        for source in inspired:
            if isinstance(source, dict):
                meta = " / ".join(str(source.get(key) or "").strip() for key in ["source", "year"] if str(source.get(key) or "").strip())
                reason = str(source.get("reason") or "").strip()
                url = str(source.get("url") or "").strip()
                suffix = f" ({meta})" if meta else ""
                details = " - ".join(part for part in [reason, url] if part)
                lines.append(f"- {_markdown_value(source.get('title', ''))}{suffix}{(' - ' + details) if details else ''}")
            else:
                lines.append(f"- {_markdown_value(source)}")
        lines.append("")
    return "\n".join(str(line) for line in lines).rstrip() + "\n"


def _project_name() -> str:
    return (
        os.environ.get("PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("DEFAULT_PROJECT_ID")
        or ""
    ).strip()


def _project_root() -> Path | None:
    project = _project_name()
    if not project:
        return None
    root = Path(os.environ.get("WORKSPACE_ROOT") or ROOT).expanduser()
    return root / "projects" / project


def _project_taste_dir() -> Path | None:
    root = _project_root()
    return root / "planning" / "finding" if root is not None else None


def _payload_run_id(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("run_id") or data.get("source_run_id") or data.get("find_run_id") or data.get("current_find_run_id") or "").strip()

def _project_current_find_run_id(project_root: Path) -> str:
    for rel in (
        Path("state/current_find_research_plan.json"),
        Path("planning/finding/find_results.json"),
        Path("planning/finding/find_progress.json"),
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



def _idea_key_for_merge(idea: dict) -> str:
    return str(idea.get("id") or idea.get("idea_id") or idea.get("title") or "").strip()


def _merge_idea_rows(base: list[dict], override: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for row in [*base, *override]:
        if not isinstance(row, dict):
            continue
        key = _idea_key_for_merge(row)
        if not key:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = row
    return [merged[key] for key in order]


def _load_patch_ideas_data(directory: Path, run_id: str) -> dict:
    runtime_data = read_json(directory / "ideas.json", {"run_id": run_id, "ideas": []})
    data = dict(runtime_data) if isinstance(runtime_data, dict) else {"run_id": run_id, "ideas": []}
    target_dir = _project_taste_dir()
    project_data = read_json(target_dir / "ideas.json", {}) if target_dir is not None else {}
    if isinstance(project_data, dict) and _payload_run_id(project_data) == run_id:
        runtime_rows = data.get("ideas", []) if isinstance(data.get("ideas"), list) else []
        project_rows = project_data.get("ideas", []) if isinstance(project_data.get("ideas"), list) else []
        merged = dict(data)
        for key, value in project_data.items():
            if key != "ideas":
                merged[key] = value
        merged["run_id"] = run_id
        merged["ideas"] = _merge_idea_rows(runtime_rows, project_rows)
        data = merged
    data.setdefault("run_id", run_id)
    data.setdefault("ideas", [])
    return data


def _idea_three_part_ready_for_state(idea: dict) -> bool:
    method = _idea_public_new_method_text(idea)
    experiment = _idea_public_initial_experiment_text(idea)
    inspired = _normalize_inspired_by(idea.get("inspired_by"), idea.get("supporting_papers") or idea.get("positive_anchor_papers"))
    return len(method.strip()) >= 40 and len(experiment.strip()) >= 40 and bool(inspired)


def _sync_project_ideas(run_id: str, data: dict, markdown: str) -> None:
    target_dir = _project_taste_dir()
    project_root = _project_root()
    if target_dir is None or project_root is None:
        return
    if not _project_sync_allowed_for_run(run_id):
        return
    write_json(target_dir / "ideas.json", data)
    write_text(target_dir / "idea.md", markdown)

    state_path = project_root / "state" / "current_find_research_plan.json"
    state = read_json(state_path, {})
    if isinstance(state, dict) and (_payload_run_id(state) in {"", run_id}):
        ideas = [row for row in data.get("ideas", []) if isinstance(row, dict)]
        now = data.get("human_supervision_updated_at") or datetime.now(timezone.utc).isoformat()
        state["run_id"] = run_id
        state["ideas"] = ideas
        state["current_find_idea_count"] = len(ideas)
        state["idea_schema_ready"] = len(ideas) >= 5 and all(_idea_three_part_ready_for_state(row) for row in ideas[:5])
        state["human_supervision_updated_at"] = now
        state["human_supervision_source"] = data.get("human_supervision_source") or "web_ideas_three_column_editor"
        write_json(state_path, state)
    idea_candidates_path = project_root / "state" / "idea_candidates.json"
    candidates = read_json(idea_candidates_path, {})
    if isinstance(candidates, dict) and (_payload_run_id(candidates) in {"", run_id}):
        candidates["run_id"] = run_id
        candidates["current_find_run_id"] = run_id
        candidates["ideas"] = data.get("ideas", [])
        candidates["human_supervision_updated_at"] = data.get("human_supervision_updated_at")
        candidates["human_supervision_source"] = data.get("human_supervision_source")
        write_json(idea_candidates_path, candidates)


def patch_idea(run_id: str, idea_id: str, patch: IdeaPatch) -> dict:
    directory = run_dir(run_id)
    data = _load_patch_ideas_data(directory, run_id)
    updates = patch.model_dump(exclude_none=True)
    if "new_method" in updates:
        updates["hypothesis"] = updates["new_method"]
        updates.setdefault("method_details", "")
        updates.setdefault("mechanism", "")
    if "method_details" in updates and "mechanism" not in updates:
        updates["mechanism"] = updates["method_details"]
    if "initial_experiment" in updates:
        updates["min_experiment"] = updates["initial_experiment"]
        updates["minimum_experiment"] = updates["initial_experiment"]
    if "min_experiment" in updates and "initial_experiment" not in updates:
        updates["initial_experiment"] = updates["min_experiment"]
        updates["minimum_experiment"] = updates["min_experiment"]
    if "inspired_by_text" in updates:
        updates["inspired_by"] = _parse_inspired_by_text(updates["inspired_by_text"])
    if updates.get("status") == "approved":
        updates["approved"] = True
        updates["approved_for_planning"] = True
        updates["pursue"] = True
    elif updates.get("status") in {"pending", "deleted"}:
        updates["approved"] = False
        updates["approved_for_planning"] = False
        updates["pursue"] = False
    now = datetime.now(timezone.utc).isoformat()
    matched = False
    for idea in data.get("ideas", []):
        if idea.get("id") == idea_id or idea.get("idea_id") == idea_id:
            idea.update(updates)
            _normalize_idea_schema(idea)
            idea["human_supervision_updated_at"] = now
            idea["human_supervision_source"] = "web_ideas_three_column_editor"
            matched = True
            break
    data["human_supervision_updated_at"] = now
    data["human_supervision_source"] = "web_ideas_three_column_editor"
    data["run_id"] = run_id
    markdown = render_ideas_markdown(data.get("ideas", []))
    write_json(directory / "ideas.json", data)
    write_text(directory / "idea.md", markdown)
    sync_latest("auto_idea", "ideas.json", directory / "ideas.json")
    sync_latest("auto_idea", "idea.md", directory / "idea.md")
    _sync_project_ideas(run_id, data, markdown)
    if not matched:
        data.setdefault("warnings", []).append({"type": "idea_not_found", "idea_id": idea_id})
    return data
