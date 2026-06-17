# Ideation / Ideas 模块

本目录是 TASTE 七阶段中的 `Ideas` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Turn reading/finding artifacts into editable research ideas without selecting an execution route.

把当前 Find/Read 证据转化成可编辑、可筛选、可实验化的研究想法。它只提出和筛选想法，不决定仓库环境，也不执行实验。

## 输入

- 当前 Find/Read 产物
- 研究主题、研究兴趣、研究者画像
- LLM API 或 Claude Code 会话
- 用户对 idea 的编辑、通过/待定/删除操作

## 输出

- ideas.json / idea.md：用户可见想法卡片和结构化状态
- hypothesis_arena.md/json：假设对比面板
- idea candidate audits：想法质量审计

## 运行逻辑

1. 从当前阅读证据提取可实验假设。
2. 生成多个想法并记录 Inspired by 的具体论文标题/来源，而不是内部 paper id。
3. 网页允许人工编辑和状态切换；只有通过的想法进入 Planning。
4. 中后期项目 Claude 调用 Ideas 时，结果只回传给 Claude，不覆盖用户初次流程的网页产物。

## 统一入口

- 公开入口：`/home/fmh/workspace/TASTE/modules/ideation/main.py`。
- 框架调用格式：`python framework/scripts/run_module.py ideation --action <action> ...`，或等价地直接调用 `python modules/ideation/main.py --action <action> ...`。
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
| `scripts/idea_pipeline.py` | 独立 idea 生成/更新主流程。 |
| `scripts/prepare_initialization.py` | 为初次项目初始化准备研究主题和基础上下文。 |

### 评估与展示

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/assess_idea_candidates.py` | 审计 idea 候选的 novelty、evidence、feasibility、experimentability、risk_control 等评分。 |
| `scripts/build_hypothesis_arena.py` | 生成可比较的假设面板，帮助选择进入 Plan 的想法。 |

## 冗余控制原则

- Ideation 脚本数量已经少，重点不是继续拆文件，而是保持网页卡片编辑、状态同步和结构化产物一致。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
