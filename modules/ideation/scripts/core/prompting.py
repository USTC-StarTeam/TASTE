from __future__ import annotations

import json
from typing import Any, Sequence

from artifact_io.artifacts import ReadingEvidence
from artifact_io.workspace import compact_text


def build_evidence_packet(evidence: Sequence[ReadingEvidence]) -> list[dict[str, Any]]:
    return [item.to_prompt_dict() for item in evidence]


def build_generation_prompt(config: dict[str, Any], evidence: Sequence[ReadingEvidence], max_ideas: int) -> str:
    topic = compact_text(config.get("research_topic") or config.get("topic"), 1000)
    interest = compact_text(config.get("research_interest"), 1600)
    profile = compact_text(config.get("researcher_profile"), 1600)
    constraints = compact_text(config.get("idea_constraints") or config.get("constraints"), 1800)
    packet = build_evidence_packet(evidence)
    title_list = [item.get("title", "") for item in packet if item.get("title")]
    return f"""
你是 TASTE ideation 模块内部的主控 Claude Code。你的唯一任务是：基于论文精读产物，生成新的、创新但可执行的论文 idea。

硬性要求：
1. 只根据下方输入证据生成 idea，不要编造不存在的论文标题、数据集、repo 或实验结果。
2. 每个 idea 必须符合研究主题、研究兴趣和研究者画像；如果证据不足，要把缺口写进风险或实验验证项。
3. 每个 idea 必须同时给出新方法、机制细节、初步最小实验、baseline/control/ablation、指标、坏例切片、停止/剪枝风险。
4. inspired_by 只能引用输入证据中的论文标题；不要写 paper id 代替标题。
5. 不要选择执行路线，不要写前端内容，不要修改文件。只输出 JSON 对象。

研究主题：
{topic or '未显式提供'}

研究兴趣：
{interest or '未显式提供'}

研究者画像：
{profile or '未显式提供'}

配置约束：
{constraints or '无额外约束'}

允许引用的证据标题：
{json.dumps(title_list, ensure_ascii=False, indent=2)}

论文精读证据包：
{json.dumps(packet, ensure_ascii=False, indent=2)}

请返回 JSON：{{"ideas": [...]}}，最多 {max_ideas} 个 idea。每个 idea 字段必须包含：id, title, one_sentence, new_method, method_details, initial_experiment, bad_case_slice, why_novel, feasibility_notes, novelty, feasibility, evidence_strength, score, risks, inspired_by。
""".strip()
