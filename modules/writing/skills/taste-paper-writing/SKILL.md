---
name: taste-paper-writing
description: TASTE Writing 的总写作规约：把实验 idea、plan、实验记录和目标 venue 转成证据受控、格式合规、引用真实的论文。Use for every main.py --action work task before drafting, repairing, compiling, or finalizing paper.tex.
---

# TASTE Paper Writing Skill

本 skill 是 `modules/writing` 的本地写作总规约。Writing 主控 Claude 必须先读本文件，再读当前项目的 selected Idea/Plan、实验 registry、records 和原始日志。生成 venue contract 后，再读 `paper/writing/venue/venue_requirements.json`、`template_source.json` 和 `paper/writing/workspace/inputs/template.tex`。

## 运行边界

- Writing 主控 Claude 的工作目录必须是 `projects/<project>/`。
- 论文、引用、venue、审计支持和 provenance 产物必须只写入 `projects/<project>/paper/writing/`。
- Writing 工作和网页对话不创建 run，也不复制项目产物。
- 正式写作完成后，`main.py` 必须启动新 Claude 进程执行独立审计；blocked 意见必须回到同一个 Writing 主控会话返修。

## 输入边界

- `idea.md`：实验 idea、核心假设、贡献候选。
- `plan.md`：实验计划、方法路线、预期对照。
- `experimental_log.md`：已经发生的实验、命令、环境、指标、失败记录、坏例。
- `records/`：实验记录表、指标表、图片、日志、补充材料。
- `venue_requirements.json`：由 venue intelligence 从当前官方来源解析出的投稿要求。
- `template.tex` 与模板 sidecar：必须来自当前官方模板或官方明确链接模板。

## 写作原则

1. 先做 venue intelligence：查找目标会议/期刊当前官方 author guidelines、submission instructions、LaTeX/Word 模板、页数/字数、匿名、AI disclosure、引用/参考文献要求。
2. 先下载并验证官方模板，再写正文。模板缺失或来源不可信时，输出阻塞报告。
3. 正文页数必须按官方规则判断：区分 body pages、reference pages、appendix、supplementary、initial submission flexible format。没有官方最小页数时，单独记录本模块质量目标。
4. 引用必须真实：优先使用 DOI、OpenAlex、Semantic Scholar、Crossref、arXiv 等可核验来源；BibTeX 与正文 citation key 必须一致。
5. 证据必须受控：实验结果只能来自输入记录和已验证记录表。缺少正结果时，写方法/理论/协议型预览稿。
6. 行文要像目标 venue 的高水平论文：明确问题张力、方法抽象、数学定义、算法或理论解释、实验协议、局限边界和可复现性。
7. 输出必须是论文正文；运行状态、审计裁决和修复目标写入 audits/provenance。

## 必需产物

- `workspace/final/paper.tex`
- `workspace/final/paper.pdf`，若本机缺 LaTeX 则保留编译报告和阻塞原因
- `workspace/refs.bib`
- `workspace/citation_pool.json`
- `workspace/provenance.json`
- `workspace/audits/*.json|md`
