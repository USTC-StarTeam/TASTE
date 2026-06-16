# Writing / Paper 模块

本目录是 TASTE 七阶段中的 `Paper/Writing` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Resolve venue requirements, draft/revise/compile the manuscript, and audit citations/figures/submission readiness from experiment evidence.

根据选中计划和实验真证据解析投稿 venue 要求，生成、修订、编译论文，并审计引用、图表、自审、声明和投稿准备度。它不能把缺失实验包装成结论。

## 输入

- venue / paper_config
- selected_plan_contract
- experiment_registry / claim ledger / metrics / bad cases
- 模板和参考文献资源
- Claude Code 写作会话或 LLM 兼容配置

## 输出

- paper.md / paper.tex / compiled PDF
- paper_pipeline.json / paper_orchestra_state.json
- claim_ledger.md/json
- figure/citation/evidence/submission audits
- review response 和修订记录

## 运行逻辑

1. 解析 venue 要求和模板。
2. 构建 claim ledger，确认每个论文声明有实验或文献证据。
3. 分阶段生成 markdown/TeX/PDF。
4. 反复审计引用、图表、公式、PDF、normality 和 submission readiness。
5. 只有证据门控通过才把内容作为可投稿稿件；预览稿必须标明未通过项。

## 文件结构

| 路径 | 作用 |
| --- | --- |
| `contracts.py` | 声明模块外部输入、输入产物、输出产物和职责边界；供框架审计和独立运行说明使用。 |
| `cli.py` | 独立模块适配入口，用于绕开网页直接以显式参数调用该模块；不能承载隐藏的 TASTE 全局状态逻辑。 |
| `script_manifest.json` | 当前脚本清单、函数、import 和归属原因；README 的脚本列表应和它保持一致。 |
| `scripts/` | 该模块真正的后端实现。新增脚本前应优先合并到下面列出的现有大块中。 |

## 脚本清单

### 主入口和生成

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/run_paper_pipeline.py` | 论文阶段主流水线。 |
| `scripts/run_paper_orchestra_bridge.py` | Claude Code/PaperOrchestra 风格写作桥，调度章节、修复和门控。 |
| `scripts/build_paper_orchestra_state.py` | 构建写作代理状态和 section 任务。 |
| `scripts/build_paper_md.py` | 生成或整理论文 Markdown。 |
| `scripts/build_conference_preview_paper.py` | 生成目标会议预览稿。 |
| `scripts/render_paper_tex.py` | 把 Markdown/结构化稿件渲染为 TeX。 |
| `scripts/compile_paper_pdf.py` | 编译 PDF 并记录 LaTeX 日志。 |

### 模板和 venue

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/resolve_venue_requirements.py` | 解析投稿 venue 的格式、页数和模板要求。 |
| `scripts/fetch_latex_template.py` | 获取/同步 LaTeX 模板。 |
| `scripts/sync_writing_vendor.py` | 同步写作 vendor 资源。 |
| `scripts/sync_third_party_research_stack.py` | 同步第三方研究/写作技能资源到 TASTE 资源结构。 |
| `scripts/paper_common.py` | 写作阶段共享路径、JSON、文本和证据工具。 |

### 审计与修复

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/build_claim_ledger.py` | 生成论文 claim ledger。 |
| `scripts/audit_paper_evidence.py` | 审计每个声明是否有真实证据支撑。 |
| `scripts/audit_paper_figures.py` | 检查图表文件、引用和渲染质量。 |
| `scripts/audit_paper_normality.py` | 检查稿件是否有异常结构、占位符或明显不自然内容。 |
| `scripts/audit_paper_orchestra.py` | 审计写作代理状态和章节完成度。 |
| `scripts/audit_submission_readiness.py` | 综合判断投稿准备度。 |
| `scripts/repair_paper_figures_loop.py` | 图表问题修复循环。 |
| `scripts/repair_paper_orchestra_citations.py` | 修复写作代理中的引用覆盖问题。 |
| `scripts/repair_paper_preview_loop.py` | 预览稿修复循环。 |
| `scripts/revise_paper_citation_coverage.py` | 提高引用覆盖度并修正缺失引用。 |
| `scripts/revise_paper_md.py` | 修订论文 Markdown。 |

### 评审与回应

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/paper_self_review.py` | 自审当前稿件。 |
| `scripts/review_paper_md.py` | 评审 Markdown 稿。 |
| `scripts/re_review_paper.py` | 复审修订后稿件。 |
| `scripts/aggregate_paper_reviews.py` | 聚合多轮评审意见。 |
| `scripts/respond_to_paper_reviews.py` | 生成审稿意见回复。 |
| `scripts/write_comparison.py` | 撰写方法/实验对比内容。 |

## 冗余控制原则

- Writing 目前脚本多但边界清楚：pipeline/orchestra、venue/template、claim/evidence audits、review/response 四块。后续可以合并 review 类脚本和 repair 类脚本；不要把 evidence gate 合进纯文本生成函数。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
