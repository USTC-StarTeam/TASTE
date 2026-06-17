# Finding / Find 模块

本目录是 TASTE 七阶段中的 `Find` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Collect, filter, score, and rank literature/tool candidates from the research topic and profile. Full-text reading evidence is explicitly outside this module.

从研究主题、研究兴趣、研究者画像和来源选择出发，抓取候选文献/代码线索，完成标题池、分类、TF-IDF 初筛、LLM 标题/摘要评分、程序性加分和最终推荐排序。Find 不做全文精读，也不把全文可用性当作前置门槛。

## 输入

- LLM API 配置或兼容客户端
- 研究主题、研究兴趣、研究者画像
- 会议/年份、arXiv/bioRxiv/Nature/Science/HuggingFace/GitHub 等来源选择
- 可选的本地 venue/cache 数据

## 输出

- find_results.json：结构化推荐、来源状态、阶段计数、分数和调试信息
- article.md：用户可读 Find 产物
- source_status.md / category_summary.json / 本地 cache：来源健康和分类统计
- planning/finding/*：同步给后续 Read/Idea/Plan 的 Find packet

## 运行逻辑

1. 生成或读取来源选择；会议优先使用带官方分类/track/类型信息的渠道，只有缺官方分类时才退到标题池筛选。
2. 构建标题总池和分类后池，使用 TF-IDF/关键词召回控制候选规模。
3. 对更多标题进行 LLM 标题评分，再抓摘要/详情做摘要评分。
4. 将 LLM 分、venue/track 类型、稳定性、多样性等程序性信号合成最终推荐分。
5. 只发布有真实标题和真实摘要证据的用户可见推荐，内部 reader 线索保留为结构化字段，不混进面向用户文案。

## 统一入口

- 公开入口：`/home/fmh/workspace/TASTE/modules/finding/main.py`。
- 框架调用格式：`python framework/scripts/run_module.py finding --action <action> ...`，或等价地直接调用 `python modules/finding/main.py --action <action> ...`。
- `scripts/` 下文件是模块私有后端实现，不应由网页前端直接拼路径调用；需要暴露时先在 `main.py` 注册 action。
- 模块契约由 `main.py --contract` 输出，不再维护单独的 `contracts.py`。

## 文件结构

| 路径 | 作用 |
| --- | --- |
| `main.py` | 本模块唯一公开后端入口；负责 action 路由，并通过 `--contract` 输出模块输入、产物和职责边界。 |
| `script_manifest.json` | 当前脚本清单、函数、import 和归属原因；README 的脚本列表应和它保持一致。 |
| `scripts/` | 该模块真正的后端实现。新增脚本前应优先合并到下面列出的现有大块中。 |

## 脚本清单

### 核心流程

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/find_pipeline.py` | Find 主流水线，负责来源聚合、阶段计数、标题/摘要评分、推荐排序和用户产物写出。 |
| `scripts/find_support.py` | 大型共享支持库，包含会议适配器、摘要/链接抓取、标题规范化、LaTeX/摘要清洗、评分辅助和来源状态构造。 |
| `scripts/literature_policy.py` | Find 阶段的来源/筛选策略常量与轻量共享规则。 |

### 来源抓取与缓存

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/build_venue_metadata_cache.py` | 为指定 venue/year 构建本地会议元数据缓存，优先保留分类、track、presentation 类型等官方信号。 |
| `scripts/build_openreview_cache.py` | 构建 OpenReview 论文索引缓存。 |
| `scripts/build_category_summary.py` | 为本地 venue/year JSON 生成中性分类摘要，供分类选择和状态展示使用。 |
| `scripts/update_local_database.py` | 刷新 TASTE 本地 venue 索引和集成数据库。 |
| `scripts/discover_arxiv.py` | arXiv API/RSS 候选抓取。 |
| `scripts/discover_semantic_scholar.py` | Semantic Scholar 候选抓取。 |
| `scripts/discover_github_repos.py` | GitHub 代码仓库候选抓取。 |
| `scripts/ingest_discovery.py` | 把外部 discover 结果归一化进入项目候选池。 |

### 计划与工具包

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/plan_literature_review.py` | 根据项目主题生成自适应文献检索计划和查询。 |
| `scripts/build_literature_tool_packet.py` | 把 Find 结果压缩成 Claude Code 可调用的 literature packet。 |
| `scripts/run_literature_tool.py` | 给项目代理使用的 literature tool 包装入口，内部调用 Find 并刷新 packet。 |

### 质量与基底审计

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/assess_paper_quality.py` | 对候选论文元数据和来源质量做确定性审计。 |
| `scripts/assess_literature_base_candidates.py` | 评估候选文献基底是否有代码、数据、正向信号和可继续跟进价值。 |
| `scripts/run_literature_base_audit.py` | 把 Find 候选送入 repo/data/env 前置审计，防止不可执行基底进入后续。 |

## 冗余控制原则

- find_pipeline.py 与 find_support.py 是核心大块，后续若拆/合只能围绕“来源适配器、评分器、产物渲染器”三类边界做，不应再新增零散 helper。
- discover_* 是不同外部 API 的薄适配器，保留分文件更清晰；共同逻辑应进入 find_support.py 或统一 source adapter 层。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
