from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from artifact_io.artifacts import ReadingEvidence, load_reading_evidence
from artifact_io.workspace import ensure_inside_ideation, make_run_dir, public_path, read_json, utc_now_iso, write_json, write_text
from claude.runner import ClaudeRunConfig, extract_payload_from_stdout, run_claude_json
from core.prompting import build_evidence_packet, build_generation_prompt
from ideation_quality.render import build_hypothesis_arena, render_ideas_markdown
from ideation_quality.schema import build_quality_audit, idea_output_schema, normalize_ideas


@dataclass(slots=True)
class StandaloneIdeationConfig:
    research_topic: str = ""
    research_interest: str = ""
    researcher_profile: str = ""
    idea_constraints: str = ""
    max_ideas: int = 6
    max_evidence_items: int = 24
    model: str = "sonnet"
    effort: str = "high"
    timeout_sec: int = 900
    max_budget_usd: float | None = None
    mock: bool = False
    strict: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "StandaloneIdeationConfig":
        allowed = {field for field in cls.__dataclass_fields__}
        clean = {key: value for key, value in dict(data or {}).items() if key in allowed}
        return cls(**clean)

    def public_dict(self) -> dict[str, Any]:
        return {
            "research_topic": self.research_topic,
            "research_interest": self.research_interest,
            "researcher_profile": self.researcher_profile,
            "idea_constraints": self.idea_constraints,
            "max_ideas": self.max_ideas,
            "max_evidence_items": self.max_evidence_items,
            "model": self.model,
            "effort": self.effort,
            "timeout_sec": self.timeout_sec,
            "strict": self.strict,
        }


def load_generation_config(config_file: str = "", config_json: str = "", overrides: dict[str, Any] | None = None) -> StandaloneIdeationConfig:
    data: dict[str, Any] = {}
    if config_file:
        data.update(json.loads(Path(config_file).expanduser().read_text(encoding="utf-8")))
    if config_json:
        candidate = Path(config_json).expanduser()
        if candidate.exists():
            data.update(json.loads(candidate.read_text(encoding="utf-8")))
        else:
            data.update(json.loads(config_json))
    if overrides:
        data.update({key: value for key, value in overrides.items() if value not in (None, "")})
    return StandaloneIdeationConfig.from_mapping(data)


def _topic_text(config: StandaloneIdeationConfig) -> str:
    return " ".join(part for part in [config.research_topic, config.research_interest, config.researcher_profile, config.idea_constraints] if part)


def _mock_payload(evidence: Sequence[ReadingEvidence], max_ideas: int) -> dict[str, Any]:
    ideas: list[dict[str, Any]] = []
    seeds = list(evidence)[: max(1, max_ideas)]
    for index, item in enumerate(seeds, 1):
        title = item.title or f"精读证据 {index}"
        ideas.append({
            "id": f"idea-{index:03d}",
            "title": f"基于《{title[:60]}》的可审计机制增强 idea",
            "one_sentence": "把精读论文中的关键机制迁移为一个可消融、可坏例审计的最小新方法。",
            "new_method": f"围绕《{title}》暴露的机制边界，提出一个证据门控的增强模块：先把论文中的核心信号、失败切片和实验协议编码成可检查条件，再只在满足条件的样本或阶段启用新增模块，避免把泛化收益误写成平均指标噪声。",
            "method_details": "方法包含证据抽取器、条件门控器、候选模块和审计记录器。训练时保持原 baseline 数据划分、负采样、随机种子和指标不变；候选模块只改变一个机制作用点，并记录启用条件、模块输出、关闭模块后的控制结果和失败样本。",
            "initial_experiment": "选择输入精读证据中最接近的 baseline 或公开 repo 作为候选基底；Environment 先验证 repo、数据和指标协议。最小实验比较 baseline、candidate、关闭门控 control、移除新增模块 ablation，报告主指标、长尾/冷启动/语义冲突切片指标、坏例样本和两次随机种子稳定性。",
            "bad_case_slice": "长尾样本、冷启动样本、语义相似但行为相反样本、baseline 高置信错误样本。",
            "why_novel": "创新点不在堆叠模块，而在把论文精读中的机制边界转化为可审计的条件启用与反例压力测试。",
            "feasibility_notes": "只要求在一个已验证基底上改动一个机制点，且所有输出都是机器可审计 JSON/Markdown，因此可行性较高。",
            "novelty": "MEDIUM",
            "feasibility": "HIGH",
            "evidence_strength": "MEDIUM",
            "score": 7.4,
            "risks": ["若输入论文没有可运行基底，需要先由 Environment 另找可复现路线。"],
            "inspired_by": [{"title": title, "source": item.source_type, "url": item.url, "reason": "精读中的机制与实验边界启发了条件门控和坏例审计设计。"}],
        })
    return {"ideas": ideas, "generation_notes": "mock 模式仅用于开发自检；正式生成必须调用 Claude Code。"}


def _render_arena_markdown(arena: dict[str, Any]) -> str:
    lines = ["# Hypothesis Arena", "", f"- 生成时间: {arena.get('generated_at', '')}", ""]
    for row in arena.get("hypotheses", []):
        lines.extend([
            f"## {row.get('hypothesis_id', '')}: {row.get('title', '')}",
            "",
            f"- priority: {row.get('priority', '')}",
            f"- method_hypothesis: {row.get('method_hypothesis', '')}",
            f"- minimal_test: {row.get('minimal_test', '')}",
            f"- counterexample_slice: {row.get('counterexample_slice', '')}",
            f"- kill_criteria: {row.get('kill_criteria', '')}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _persist_generation_outputs(
    run_dir: Path,
    raw_payload: dict[str, Any],
    claude_meta: dict[str, Any],
    config: StandaloneIdeationConfig,
    evidence_titles: Sequence[str],
) -> dict[str, Any]:
    write_json(run_dir / "claude_payload.json", raw_payload)
    ideas = normalize_ideas(raw_payload, evidence_titles, config.max_ideas)
    if not ideas:
        raise RuntimeError("Claude Code 没有生成可用 idea。")
    audit = build_quality_audit(ideas, evidence_titles, _topic_text(config))
    arena = build_hypothesis_arena(ideas, _topic_text(config))
    payload = {
        "run_id": run_dir.name,
        "generated_at": utc_now_iso(),
        "source": "ideation_standalone_claude_code",
        "config": config.public_dict(),
        "ideas": ideas,
        "quality_audit": audit,
        "claude": claude_meta,
    }
    write_json(run_dir / "ideas.json", payload)
    write_text(run_dir / "idea.md", render_ideas_markdown(ideas, audit))
    write_json(run_dir / "idea_quality_audit.json", audit)
    write_json(run_dir / "hypothesis_arena.json", arena)
    write_text(run_dir / "hypothesis_arena.md", _render_arena_markdown(arena))
    manifest = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "artifacts": ["input_bundle.json", "claude_prompt.md", "claude_payload.json", "ideas.json", "idea.md", "idea_quality_audit.json", "hypothesis_arena.json", "hypothesis_arena.md"],
        "all_outputs_inside_ideation": True,
    }
    write_json(run_dir / "manifest.json", manifest)
    if config.strict and audit.get("has_blocking_issue"):
        raise RuntimeError(f"idea 质量门未通过，详见 {run_dir / 'idea_quality_audit.json'}")
    return {
        "status": "ok",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "artifacts": {name: str(run_dir / name) for name in manifest["artifacts"]},
        "public_artifacts": {name: public_path(run_dir / name) for name in manifest["artifacts"]},
        "idea_count": len(ideas),
        "quality_passed_count": audit.get("passed_count", 0),
        "called_claude_code": bool(claude_meta.get("called_claude_code")),
    }


def run_standalone_ideation(
    input_paths: Sequence[str],
    config: StandaloneIdeationConfig,
    run_id: str = "",
    output_root: str = "",
) -> dict[str, Any]:
    if not input_paths:
        raise ValueError("必须提供至少一个论文精读产物路径。")
    run_dir = make_run_dir(run_id, output_root or None)
    evidence = load_reading_evidence(input_paths, max_items=config.max_evidence_items)
    evidence_titles = [item.title for item in evidence if item.title]
    bundle = {
        "generated_at": utc_now_iso(),
        "config": config.public_dict(),
        "input_paths": list(input_paths),
        "evidence": build_evidence_packet(evidence),
    }
    write_json(run_dir / "input_bundle.json", bundle)
    prompt = build_generation_prompt(config.public_dict(), evidence, config.max_ideas)
    write_text(run_dir / "claude_prompt.md", prompt + "\n")
    if config.mock:
        raw_payload = _mock_payload(evidence, config.max_ideas)
        claude_meta = {"mode": "mock", "called_claude_code": False}
    else:
        raw_payload, claude_meta = run_claude_json(
            prompt,
            idea_output_schema(config.max_ideas),
            run_dir,
            ClaudeRunConfig(model=config.model, effort=config.effort, timeout_sec=config.timeout_sec, max_budget_usd=config.max_budget_usd),
        )
        claude_meta["called_claude_code"] = True
    return _persist_generation_outputs(run_dir, raw_payload, claude_meta, config, evidence_titles)


def finalize_standalone_run(run_dir: str | Path, strict: bool = False) -> dict[str, Any]:
    target = ensure_inside_ideation(Path(run_dir).expanduser())
    if not target.is_dir():
        raise FileNotFoundError(f"run 目录不存在：{target}")
    bundle = read_json(target / "input_bundle.json", {})
    if not isinstance(bundle, dict) or not bundle.get("evidence"):
        raise ValueError(f"缺少可用于 finalize 的 input_bundle.json：{target}")
    config = StandaloneIdeationConfig.from_mapping(bundle.get("config", {}))
    if strict:
        config.strict = True
    evidence_titles = [
        str(item.get("title", "")).strip()
        for item in bundle.get("evidence", [])
        if isinstance(item, dict) and str(item.get("title", "")).strip()
    ]
    stdout_path = target / "claude_stdout.json"
    if not stdout_path.exists():
        raise FileNotFoundError(f"缺少 Claude stdout：{stdout_path}")
    raw_payload = extract_payload_from_stdout(stdout_path.read_text(encoding="utf-8"))
    command_payload = read_json(target / "claude_command.json", {})
    claude_meta = command_payload.get("meta", {}) if isinstance(command_payload, dict) else {}
    if not isinstance(claude_meta, dict):
        claude_meta = {}
    claude_meta.update({"called_claude_code": True, "finalized_from_stdout": True, "stdout_path": str(stdout_path)})
    return _persist_generation_outputs(target, raw_payload, claude_meta, config, evidence_titles)
