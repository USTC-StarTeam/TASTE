# Planning / Plan 模块

本目录是 TASTE 七阶段中的 `Plan` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Select and repair executable research plans from approved ideas; downstream modules consume only explicit selected plan contracts.

把已通过的 ideas 细化成候选实验计划和唯一执行合同。Planning 可以说明应该用什么 repo、如何改、如何评估；但不能假定 Environment 已经确认基底，更不能显示“待环境 gate 确认的基底”这类反向依赖文案。

## 输入

- 通过的 ideas.json/idea.md
- 项目约束和用户选择
- 当前 Find/Read 证据
- LLM API 或 Claude Code 会话

## 输出

- plans.json / plan.md：候选计划和最终计划正文
- experiment_plan.json / taste_plan_bridge.json：供 Environment/Experiment 消费的执行合同
- blocker_action_plan.json：阻塞时的行动计划

## 运行逻辑

1. 读取用户显式选择的 idea。
2. 生成候选计划，细化方法改动、仓库/数据需求、指标和验证路径。
3. 执行计划评估与修复，保留内部审计但用户框内只显示可读计划正文。
4. 生成唯一执行合同，后续 Environment 依据合同选择/验证 repo 和数据。

## 统一入口

- 公开入口：`/home/fmh/workspace/TASTE/modules/planning/main.py`。
- 框架调用格式：`python framework/scripts/run_module.py planning --action <action> ...`，或等价地直接调用 `python modules/planning/main.py --action <action> ...`。
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
| `scripts/plan_pipeline.py` | 独立计划生成主流程，对外由 `main.py --action plan/pipeline` 调用。 |

### 私有工具集合

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/planning_tools.py` | Planning 私有工具集合；承载 `experiments`、`workflow`、`blocker_resolution`、`review_board`、`method_frontier`、`reflect` 等 tool action。 |
| `scripts/propose_next_actions.py` | 根据当前状态提出下一步行动；测试会直接 import 其评分逻辑，因此暂保留为独立私有实现，对外仍走 `main.py --action next_actions`。 |
| `scripts/build_blocker_action_plan.py` | 根据阻塞状态生成结构化行动计划，并引用 Claude skill 资源；对外走 `main.py --action blocker_action`。 |

## 冗余控制原则

- Plan 的公开动作必须收敛到 `main.py`；同类小工具已经合并到 `planning_tools.py`，后续新增小工具优先扩展 tool action，而不是新增单文件入口。
- 禁止把 Environment 的结果提前写进 Plan；Plan 只能提出需求和建议，由 Environment 验证。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
