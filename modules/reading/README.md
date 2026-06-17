# Reading / Read 模块

本目录是 TASTE 七阶段中的 `Read` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Acquire verified paper-body text for the selected Find packet and synthesize reading notes. Same-run replacements for unavailable public full text happen here, never inside Finding.

从当前选中的 Find run 读取推荐论文，获取可核查全文/摘要/链接证据，生成面向研究者的精读结果和后续 Idea/Plan 的内部证据包。全文抓取、全文替代和阅读证据修复都属于 Reading，不属于 Finding。

## 输入

- 当前 Find packet：find_results.json、article.md、推荐列表和链接
- LLM API 或 Claude Code 项目会话
- 项目研究画像和用户可见选择
- 可选人工全文来源 full_text_reading/manual_full_text_sources.json

## 输出

- read_results.json / read.md：精读结果、方法要点、证据边界
- full_text_reading/full_text_packet.json：全文/摘要证据包
- current_find_full_text_evidence_repair.json：同轮全文证据修复审计
- Ideas/Plan 可消费的当前 Find 研究计划状态

## 运行逻辑

1. 锁定当前 Find run，防止历史 run 覆盖当前产物。
2. 读取推荐论文真实摘要、链接和可用全文；缺全文时在 Read 阶段处理，不反向污染 Find。
3. 生成用户可读精读和结构化证据，内部 reader 指令保留在结构化字段。
4. 触发 Idea/Plan 时只使用当前 Find 对应的 Read 证据。

## 统一入口

- 公开入口：`/home/fmh/workspace/TASTE/modules/reading/main.py`。
- 框架调用格式：`python framework/scripts/run_module.py reading --action <action> ...`，或等价地直接调用 `python modules/reading/main.py --action <action> ...`。
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
| `scripts/read_pipeline.py` | 独立 Read 主入口，读取 Find packet 并产出精读结构。 |
| `scripts/ensure_current_find_research_plan.py` | 当前网页主路由：保证 Read/Ideas/Plan 使用最新选中 Find run，并驱动 Claude Code 项目会话生成阅读、想法和计划。 |

### 导入与修复

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/repair_current_find_full_text_evidence.py` | 在 Read 阶段修复当前 Find 的全文证据包；不允许把全文检查移回 Find。 |

单篇外部论文导入由 `main.py --action import` 直接处理，不再保留单独私有脚本。

## 冗余控制原则

- ensure_current_find_research_plan.py 目前承担当前 Find 到 Read/Ideas/Plan 的大块编排，后续应拆成“当前 Find 锁定、Claude 会话、产物渲染”三个明确内部模块后再合并接口。
- Reading 不能新增只为某一篇论文服务的全文补丁脚本。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
