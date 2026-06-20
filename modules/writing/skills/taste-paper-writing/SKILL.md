---
name: taste-paper-writing
description: TASTE writing_dev 的总写作规约：把实验 idea、plan、实验记录和目标 venue 转成证据受控、格式合规、引用真实的论文。
---

# TASTE Paper Writing Skill

本 skill 是 `modules/writing_dev` 的本地写作总规约。Claude Code 写论文时必须先读本文件，再读当前 run 的 `workspace/inputs/`、`venue/venue_requirements.json`、`venue/template_source.json` 和 `workspace/inputs/template.tex`。

## 输入边界

- `idea.md`：实验 idea、核心假设、贡献候选。
- `plan.md`：实验计划、方法路线、预期对照。
- `experimental_log.md`：已经发生的实验、命令、环境、指标、失败记录、坏例。
- `records/`：实验记录表、指标表、图片、日志、补充材料。
- `venue_requirements.json`：由 venue intelligence 从当前官方来源解析出的投稿要求。
- `template.tex` 与模板 sidecar：从当前官方模板下载或同步，不允许用本地极简模板冒充官方模板。

## 写作原则

1. 先做 venue intelligence：查找目标会议/期刊当前官方 author guidelines、submission instructions、LaTeX/Word 模板、页数/字数、匿名、AI disclosure、引用/参考文献要求。
2. 先下载并验证官方模板，再写正文。模板缺失或来源不可信时，只能输出阻塞报告，不能生成“看起来像官方”的论文。
3. 正文页数必须按官方规则判断：区分 body pages、reference pages、appendix、supplementary、initial submission flexible format。没有官方最小页数时，用本模块质量目标，不要把质量目标说成官方规则。
4. 引用必须真实：优先使用 DOI、OpenAlex、Semantic Scholar、Crossref、arXiv 等可核验来源；BibTeX 与正文 citation key 必须一致；不得虚构论文、作者、年份、venue。
5. 证据必须受控：实验结果只能来自输入记录和已验证记录表。缺少正结果时，可以写方法/理论/协议型预览稿，但不能伪造成实证突破。
6. 行文要像目标 venue 的高水平论文：明确问题张力、方法抽象、数学定义、算法或理论解释、实验协议、局限边界和可复现性。
7. 输出必须是论文，不是计划书、审计报告、项目状态说明或 gate report。必要的限制只能以 manuscript-native limitations / scope wording 表达。

## 必需产物

- `workspace/final/paper.tex`
- `workspace/final/paper.pdf`，若本机缺 LaTeX 则保留编译报告和阻塞原因
- `workspace/refs.bib`
- `workspace/citation_pool.json`
- `workspace/provenance.json`
- `workspace/audits/*.json|md`
