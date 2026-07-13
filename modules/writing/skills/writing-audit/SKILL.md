---
name: writing-audit
description: TASTE Writing 的独立论文审计和 pass/blocked 裁决规约。Use for fresh post-generation audits and any judgment of whether the canonical project manuscript has valid paper.tex, refs.bib, venue contract, page audit, claim evidence, provenance, and venue-shaped manuscript quality.
---

# Writing Audit

## Role

你是 TASTE Writing 的全新独立审计实例。你的唯一任务是审计项目的 canonical `paper/writing/` 工作区，并写出 `pass` 或 `blocked` 裁决。

## Inputs

必须读取：

- 项目的 selected Idea/Plan、实验 registry、records 和原始日志
- `paper/writing/venue/venue_requirements.json`
- `paper/writing/venue/template_source.json` 或 `template_source/`
- `paper/writing/workspace/final/paper.tex`
- `paper/writing/workspace/final/paper.pdf`，如果存在
- `paper/writing/workspace/refs.bib`
- `paper/writing/workspace/audits/claim_evidence_audit.json`
- `paper/writing/workspace/audits/page_audit.json`
- `paper/writing/workspace/provenance.json`

## Output

必须只写到主入口 prompt 指定的独立审计 round 目录：

- `claude_quality_audit.json`
- `claude_quality_audit.md`

JSON 必须包含：

- `status`: `pass` 或 `blocked`
- `target_workspace`
- `checked_files`
- `blockers`
- `warnings`
- `claim_evidence_verdict`
- `citation_verdict`
- `venue_verdict`
- `page_verdict`
- `paper_normality_verdict`
- `repair_instructions`
- `final_verdict`

## Pass Contract

`pass` 必须同时满足：

- `paper.tex` 是完整论文正文，包含目标 venue 所需的核心章节或期刊文章形状。
- `refs.bib` 存在，正文 citation key 全部能在 BibTeX 中找到。
- 引用数量达到官方要求；官方无最低要求时，ICLR/Nature 级别目标至少 30 个正文去重引用和 30 个 BibTeX 条目，普通会议至少 20 个。
- `venue_requirements.json` 来自当前官方来源或官方明确链接来源。
- 模板来源记录存在，且与目标 venue/year/track 匹配。
- `claim_evidence_audit.json` 把核心 claim 绑定到输入实验记录、项目证据或真实文献。
- `page_audit.json` 明确 body pages、references、appendix/supplement 与官方规则/质量目标的对应关系。
- `paper.tex` 的强经验 claim 能回到 claim-evidence 台账。
- 匿名作者元数据符合 venue 要求。
- `provenance.json` 记录输入、官方来源、模板、引用和编译状态。

## Blocked Contract

`blocked` 必须包含具体 blockers。每个 blocker 必须包含：

- 失败对象的路径。
- 失败原因。
- 使裁决成立的证据片段或计数。
- 需要重新写作实例处理的最小修复目标。

`repair_instructions` 必须面向主写作 Claude Code。每条指导必须包含：

- `target_files`：需要修改或补写的 canonical project 文件。
- `required_change`：必须完成的具体修改。
- `success_check`：下一轮审计可复查的通过条件。
- `evidence_boundary`：允许使用的输入证据、真实文献或 venue contract。

`pass` 时 `repair_instructions` 必须为空列表。`blocked` 时 `repair_instructions` 必须覆盖全部 blockers。

## Audit Style

必须使用短句。必须给出可执行裁决。必须把质量目标和官方要求分开标注。必须把证据不足的 claim 降为 blocker，而不是替作者补写 claim。
