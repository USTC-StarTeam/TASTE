from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from artifact_io.workspace import existing_run_dir, make_run_dir, read_json, refresh_latest_run, write_json, write_text
from claude.runner import ClaudeRunConfig, run_claude_markdown
from ideation_quality.render import render_ideas_markdown
from ideation_quality.schema import build_quality_audit, ideas_from_markdown, markdown_contract_issues


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
MAX_IDEAS = 50
MAX_INPUT_BYTES = 10_000_000


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise RuntimeError("Ideation cancelled by caller.")


def _config_topic_text(config: dict | None) -> str:
    config = config if isinstance(config, dict) else {}
    parts = [str(config.get("research_interest") or ""), str(config.get("researcher_profile") or "")]
    selection = config.get("default_find_selection") if isinstance(config.get("default_find_selection"), dict) else {}
    parts.extend(str(selection.get(key) or "") for key in ("topic", "research_interest", "user_prompt"))
    return " ".join(part for part in parts if part).strip()


def _topic_terms(config: dict | None) -> list[str]:
    ignored = {"research", "paper", "model", "method", "data", "dataset", "system", "experiment", "baseline"}
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", _config_topic_text(config).lower()):
        if token not in ignored and token not in terms:
            terms.append(token)
    return terms[:40]


def _select_relevant_items(items: list[dict], limit: int, config: dict | None) -> list[dict]:
    terms = _topic_terms(config)

    def rank(item: dict) -> tuple[int, float]:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        try:
            score = float(item.get("score") or item.get("fit_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        return sum(1 for term in terms if term in text), score

    return sorted((item for item in items if isinstance(item, dict)), key=rank, reverse=True)[:limit]


def _build_prompt(bundle: dict, config: dict, max_ideas: int) -> str:
    items = [item for item in bundle.get("items", []) if isinstance(item, dict)]
    selected = _select_relevant_items(items, max(12, max_ideas * 4), config)
    return f"""
你是 TASTE Ideation 模块的 Claude Code。直接生成最终用户审阅的 `idea.md` Markdown 正文，不要输出 JSON、代码围栏或解释性前后缀。

硬性要求：
1. 只基于下方 Framework 提供的当前 Find/Read 证据生成想法，不编造论文、repo、数据集或实验结果。已有工作的能力、局限、数据和数字事实必须能对应输入证据，无法对应就删除。
2. 每个想法必须写清：新方法、机制细节、初步最小实验、baseline/control/ablation、指标和停止标准。
3. “初步实验”必须说明基于哪项工作或可审计基底、最小改动是什么、Environment 需要验证什么。
4. 禁止出现“待补齐”“待项目代理”“TODO”“TBD”等占位文本；证据缺口写进风险与停止标准。
5. 启发来源只能引用允许引用的证据标题。
6. 只生成候选想法，不选择执行路线，不运行环境或实验。
7. 提交前自行检查 Markdown 标题层级；数学公式使用成对 `$...$` 或 `$$...$$`；输入证据提供 URL 时，网页引用必须使用 `[标题](https://...)`，没有 URL 时只写证据标题，严禁编造链接。
8. 输出前删掉所有模板尖括号占位。检查过程不要写入正文，禁止输出 `### 自检` 或任何 pass/fail 检查清单。
9. 每个 idea 必须按固定顺序各写一次 `### 新方法`、`### 机制细节`、`### 初步实验`、`### 启发来源`、`### 风险与停止标准`。
10. 新方法、初步实验和停止标准属于研究提案，必须使用提案口径，不得声称已经实现、运行、观察或验证。风险中的事实判断只能来自输入证据明确记录的局限；无直接风险证据时只写提案的停止规则，不得猜测失败原因、样本分组、失败现象或结果。
11. 实际 idea 数量必须在 1 到 {max_ideas} 之间，页首 `idea 数量` 必须等于实际二级标题数量。每个 idea 都要写完整元数据，`id` 必须唯一，`status` 只能是 pending，三项等级只能是 HIGH/MEDIUM/LOW。

固定格式如下，栏目名不得修改：

# Ideation 生成的新论文想法

- 生成时间: {datetime.now(timezone.utc).isoformat()}
- idea 数量: <数量>

## 1. <idea 标题>

- id: `idea-001`
- status: pending
- score: <0-10>
- novelty: HIGH/MEDIUM/LOW
- feasibility: HIGH/MEDIUM/LOW
- evidence_strength: HIGH/MEDIUM/LOW

### 新方法
<详细描述。>

### 机制细节
<输入、模型或算法改动、训练或推理作用点、机制依据。>

### 初步实验
<基底、最小改动、baseline/control/ablation、指标和 Environment 验证项。>

### 启发来源
- [<证据标题>](<输入中已有的 URL>) - <具体启发；无 URL 时使用纯文本标题>

### 风险与停止标准
<输入证据明确记录的风险，以及提案的停止/剪枝规则；无直接风险证据时不要推测失败原因。>

研究兴趣：
{str(config.get('research_interest') or '未显式提供')}

研究者画像：
{str(config.get('researcher_profile') or '未显式提供')}

当前 Find/Read 摘要证据：
{json.dumps(selected, ensure_ascii=False, indent=2)}

read.md：
{str(bundle.get('read_markdown') or '')[:120_000]}

最多生成 {max_ideas} 个想法。直接输出完整 `idea.md`。
""".strip()


def _claude_config_from_env() -> ClaudeRunConfig:
    return ClaudeRunConfig(
        model=os.environ.get("IDEATION_CLAUDE_MODEL", "sonnet"),
        effort=os.environ.get("IDEATION_CLAUDE_EFFORT", "high"),
        timeout_sec=int(os.environ.get("IDEATION_CLAUDE_TIMEOUT_SEC") or os.environ.get("IDEA_TIMEOUT_SEC") or "1200"),
    )


def _evidence_titles(bundle: dict) -> list[str]:
    return [
        str(item.get("title") or "").strip()
        for item in bundle.get("items", [])
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ]


def _validate_bundle(bundle: object, run_id: str) -> dict:
    if not isinstance(bundle, dict):
        raise ValueError("Ideation input must be one normalized JSON object.")
    if bundle.get("schema_version") != "taste.ideation_input.v1":
        raise ValueError("Ideation input schema_version must be taste.ideation_input.v1.")
    if str(bundle.get("run_id") or "").strip() != str(run_id or "").strip():
        raise ValueError("Ideation input run_id does not match --run-id.")
    items = bundle.get("items")
    if not isinstance(items, list) or not any(isinstance(item, dict) and str(item.get("title") or "").strip() for item in items):
        raise ValueError("Ideation input items must contain at least one titled evidence item.")
    if not isinstance(bundle.get("read_markdown"), str):
        raise ValueError("Ideation input must contain a normalized read_markdown field.")
    return bundle


def _write_manifest(run_dir: Path, source_run_id: str, action: str) -> None:
    artifacts = [
        name
        for name in (
            "input_bundle.json",
            "claude_prompt.md",
            "claude_command.json",
            "claude_stdout.json",
            "claude_stderr.log",
            "claude_repair_command.json",
            "claude_repair_stdout.json",
            "claude_repair_stderr.log",
            "idea.md",
            "ideas.json",
        )
        if (run_dir / name).exists()
    ]
    current = read_json(run_dir / "manifest.json", {})
    write_json(run_dir / "manifest.json", {
        "run_id": run_dir.name,
        "source_run_id": source_run_id,
        "action": action,
        "created_at": current.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "public_final_artifact": "idea.md",
        "machine_support_artifacts": ["ideas.json"],
        "artifacts": artifacts,
        "latest_run_is_review_copy_only": True,
    })


def _persist_markdown(
    run_dir: Path,
    source_run_id: str,
    markdown: str,
    *,
    config: dict | None,
    generation_trace: dict | None = None,
    action: str,
    strict_quality: bool,
    max_ideas: int = 0,
) -> dict:
    bundle = read_json(run_dir / "input_bundle.json", {})
    titles = _evidence_titles(bundle if isinstance(bundle, dict) else {})
    ideas = ideas_from_markdown(markdown, titles, max(1, 1000))
    if not ideas:
        raise ValueError("idea.md does not contain any parseable idea sections.")
    evidence_items = bundle.get("items", []) if isinstance(bundle, dict) and isinstance(bundle.get("items"), list) else []
    contract_issues = markdown_contract_issues(markdown, ideas, evidence_items, max_ideas=max_ideas)
    audit = build_quality_audit(ideas, titles, _config_topic_text(config))
    audit["markdown_contract_issues"] = contract_issues
    audit["has_blocking_issue"] = bool(audit.get("has_blocking_issue") or contract_issues)
    if contract_issues:
        raise ValueError("idea.md failed its Markdown contract: " + "; ".join(contract_issues[:8]))
    if strict_quality and audit.get("has_blocking_issue"):
        issues = [str(issue) for row in audit.get("items", []) for issue in row.get("issues", [])]
        raise ValueError("idea.md failed its quality gate: " + "; ".join(issues[:8]))
    existing = read_json(run_dir / "ideas.json", {})
    payload = dict(existing) if isinstance(existing, dict) else {}
    payload.update({
        "run_id": source_run_id,
        "source_run_id": source_run_id,
        "ideation_run_id": run_dir.name,
        "run_dir": str(run_dir),
        "source": "taste_ideation",
        "public_final_artifact": "idea.md",
        "machine_projection_from": "idea.md",
        "ideas": ideas,
        "quality_audit": audit,
    })
    if generation_trace is not None:
        payload["generation_trace"] = generation_trace
    if action != "idea":
        now = datetime.now(timezone.utc).isoformat()
        payload["human_supervision_updated_at"] = now
        payload["human_supervision_source"] = "web_idea_markdown_editor"
    write_text(run_dir / "idea.md", markdown.rstrip() + "\n")
    write_json(run_dir / "ideas.json", payload)
    _write_manifest(run_dir, source_run_id, action)
    return payload


def _mock_ideas(items: list[dict], max_ideas: int) -> list[dict]:
    ideas: list[dict] = []
    for index, item in enumerate(items[:max_ideas], 1):
        title = str(item.get("title") or f"证据 {index}")
        ideas.append({
            "id": f"idea-{index:03d}",
            "title": f"基于《{title[:70]}》的可审计机制增强",
            "status": "pending",
            "score": 7.5,
            "novelty": "MEDIUM",
            "feasibility": "HIGH",
            "evidence_strength": "MEDIUM",
            "new_method": f"围绕《{title}》中可验证的机制边界，提出一个条件启用且可关闭的最小增强模块，并记录其触发条件和输出，使整体指标与具体机制证据可以分开审计。",
            "method_details": "保持原数据划分、训练预算、随机种子和评价脚本不变，只在一个明确的训练或推理作用点加入条件门控与审计记录器。记录器逐样本保存触发条件、门控值和输出变化；模块关闭时严格退化为原始基线，并用等参数 control 隔离额外容量、训练噪声与日志开销。",
            "initial_experiment": "以该证据对应的公开实现或同协议可运行基底为 baseline，只增加一个可关闭模块；比较 baseline、candidate、关闭模块的 control 和移除关键组件的 ablation，报告主指标、运行成本和至少两个随机种子，并由 Environment 先验证 repo、数据划分和评价脚本一致。",
            "inspired_by": [{"title": title, "source": str(item.get("source") or "reading"), "url": str(item.get("url") or ""), "reason": "其机制边界启发了条件启用和逐样本审计。"}],
            "risks": ["若同协议 baseline/control/ablation 下没有稳定收益，则停止该方向。"],
        })
    return ideas


def run_idea(
    run_id: str,
    max_ideas: int,
    config: dict,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
    *,
    input_json: str | Path,
    mock: bool = False,
) -> dict:
    config = config if isinstance(config, dict) else {}
    run_dir = make_run_dir()
    try:
        input_path = Path(input_json).expanduser()
        if input_path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError(f"Ideation input exceeds {MAX_INPUT_BYTES} bytes.")
        bundle = _validate_bundle(
            json.loads(input_path.read_text(encoding="utf-8")),
            run_id,
        )
        write_json(run_dir / "input_bundle.json", bundle)
        _raise_if_cancelled(should_cancel)
        items = [item for item in bundle.get("items", []) if isinstance(item, dict)]
        max_ideas = min(MAX_IDEAS, max(1, int(max_ideas or config.get("max_ideas") or 1)))
        prompt = _build_prompt(bundle, config, max_ideas)
        write_text(run_dir / "claude_prompt.md", prompt + "\n")

        if mock:
            mock_ideas = _mock_ideas(items, max_ideas)
            audit = build_quality_audit(mock_ideas, _evidence_titles(bundle), _config_topic_text(config))
            markdown = render_ideas_markdown(mock_ideas, audit)
            trace = {"mode": "mock", "called_claude_code": False}
        else:
            markdown, claude_meta = run_claude_markdown(prompt, run_dir, _claude_config_from_env())
            trace = {"mode": "claude_code_direct_markdown", "called_claude_code": True, "claude": claude_meta}
        _raise_if_cancelled(should_cancel)
        try:
            payload = _persist_markdown(
                run_dir,
                run_id,
                markdown,
                config=config,
                generation_trace=trace,
                action="idea",
                strict_quality=True,
                max_ideas=max_ideas,
            )
        except ValueError as exc:
            if mock:
                raise
            _raise_if_cancelled(should_cancel)
            evidence_reference = [
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                }
                for item in items
                if str(item.get("title") or "").strip()
            ]
            repair_prompt = f"""
你刚生成的 `idea.md` 未通过确定性检查：

{exc}

请直接返回修正后的完整 `idea.md`，不要输出解释、JSON 或代码围栏。保留有证据支撑的具体内容；每个 idea 必须严格包含且只包含一次以下三级标题，并按此顺序排列：

### 新方法
### 机制细节
### 初步实验
### 启发来源
### 风险与停止标准

不得出现 `### 自检`、`### 坏例切片`、`### 重点验证场景`、pass/fail 检查清单、待补齐/TODO/TBD/尖括号模板占位；数学公式定界符必须成对；有 URL 的引用必须是 Markdown 链接。删除没有输入证据支撑的事实、数字、样本分组、失败原因和失败现象；无直接风险证据时只保留提案的停止规则，研究提案不得冒充已完成结果。请在提交前静默完成检查，只返回修正后的正文。修正这份原稿：

实际 idea 数量必须在 1 到 {max_ideas} 之间，页首数量必须准确；每个 idea 的 id 必须合法且唯一，status/score/novelty/feasibility/evidence_strength 元数据必须完整。启发来源只能使用以下精确标题和对应 URL；无法修正的引用直接删除：

{json.dumps(evidence_reference, ensure_ascii=False, indent=2)}

{markdown}
""".strip()
            repaired_markdown, repair_meta = run_claude_markdown(
                repair_prompt,
                run_dir,
                _claude_config_from_env(),
                artifact_prefix="claude_repair",
            )
            _raise_if_cancelled(should_cancel)
            trace["repair"] = {"reason": str(exc), "claude": repair_meta}
            payload = _persist_markdown(
                run_dir,
                run_id,
                repaired_markdown,
                config=config,
                generation_trace=trace,
                action="idea",
                strict_quality=True,
                max_ideas=max_ideas,
            )
        log(f"Ideation generated idea.md with {len(payload['ideas'])} ideas.")
        return {
            "status": "ok",
            "run_id": run_id,
            "source_run_id": run_id,
            "ideation_run_id": run_dir.name,
            "run_dir": str(run_dir),
            "public_final_artifact": str(run_dir / "idea.md"),
            "idea_count": len(payload["ideas"]),
        }
    finally:
        refresh_latest_run(run_dir)


def _idea_blocks(markdown: str) -> list[tuple[int, int, str, str]]:
    matches = list(re.finditer(r"^##\s+(?:\d+\.\s*)?(.+?)\s*$", markdown, flags=re.MULTILINE))
    blocks: list[tuple[int, int, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        block = markdown[match.start():end]
        id_match = re.search(r"^-\s*id\s*:\s*`?([^`\s]+)`?\s*$", block, flags=re.MULTILINE | re.IGNORECASE)
        blocks.append((match.start(), end, id_match.group(1).strip() if id_match else "", match.group(1).strip()))
    return blocks


def _replace_section_body(block: str, heading: str, value: str) -> str:
    pattern = re.compile(
        rf"(^###\s+{re.escape(heading)}\s*$).*?(?=^###\s+|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    if not pattern.search(block):
        raise ValueError(f"idea.md section not found: {heading}")
    return pattern.sub(lambda match: f"{match.group(1)}\n{value.strip()}\n\n", block, count=1)


def _patch_markdown(markdown: str, idea_id: str, updates: dict) -> str:
    blocks = _idea_blocks(markdown)
    selected = next((item for item in blocks if idea_id in {item[2], item[3]}), None)
    if selected is None:
        raise KeyError(f"Idea not found in idea.md: {idea_id}")
    start, end, _parsed_id, _title = selected
    block = markdown[start:end]
    if "title" in updates:
        title = str(updates["title"]).strip()
        if not title:
            raise ValueError("Idea title cannot be empty.")
        block = re.sub(
            r"^(##\s+(?:\d+\.\s*)?).+$",
            lambda match: match.group(1) + title,
            block,
            count=1,
            flags=re.MULTILINE,
        )
    for key, heading in {"new_method": "新方法", "initial_experiment": "初步实验"}.items():
        if key in updates:
            block = _replace_section_body(block, heading, str(updates[key]))
    if "status" in updates:
        status = str(updates["status"]).strip()
        if status not in {"pending", "approved", "deleted"}:
            raise ValueError(f"Unsupported idea status: {status}")
        status_line = f"- status: {status}"
        if re.search(r"^-\s*status\s*:.*$", block, flags=re.MULTILINE | re.IGNORECASE):
            block = re.sub(r"^-\s*status\s*:.*$", status_line, block, count=1, flags=re.MULTILINE | re.IGNORECASE)
        else:
            block = re.sub(r"(^-\s*id\s*:.*$)", rf"\1\n{status_line}", block, count=1, flags=re.MULTILINE | re.IGNORECASE)
    return markdown[:start] + block.rstrip() + "\n\n" + markdown[end:].lstrip("\n")


def _run_result(directory: Path, payload: dict) -> dict:
    return {
        "status": "ok",
        "run_id": str(payload.get("run_id") or ""),
        "source_run_id": str(payload.get("source_run_id") or payload.get("run_id") or ""),
        "ideation_run_id": directory.name,
        "run_dir": str(directory.resolve()),
        "public_final_artifact": str((directory / "idea.md").resolve()),
        "idea_count": len(payload.get("ideas") or []),
    }


def update_idea_markdown(
    run_dir: str | Path,
    run_id: str,
    markdown: str,
    config: dict | None = None,
    *,
    action: str = "update_markdown",
) -> dict:
    directory = existing_run_dir(run_dir)
    existing = read_json(directory / "ideas.json", {})
    existing_run_id = str(existing.get("run_id") or existing.get("source_run_id") or "").strip() if isinstance(existing, dict) else ""
    if existing_run_id and existing_run_id != str(run_id or "").strip():
        raise ValueError(f"Ideation edit run_id {run_id} does not match existing run source {existing_run_id}.")
    try:
        payload = _persist_markdown(
            directory,
            run_id,
            markdown,
            config=config,
            action=action,
            strict_quality=False,
        )
        return _run_result(directory, payload)
    finally:
        refresh_latest_run(directory)


def patch_idea(run_dir: str | Path, run_id: str, idea_id: str, patch: dict, config: dict | None = None) -> dict:
    directory = existing_run_dir(run_dir)
    markdown = (directory / "idea.md").read_text(encoding="utf-8")
    if not isinstance(patch, dict):
        raise ValueError("Idea patch must be a JSON object.")
    updates = {
        key: patch[key]
        for key in ("title", "new_method", "initial_experiment", "status")
        if patch.get(key) is not None
    }
    if not updates:
        return _run_result(directory, read_json(directory / "ideas.json", {}))
    return update_idea_markdown(directory, run_id, _patch_markdown(markdown, idea_id, updates), config=config, action="patch")
