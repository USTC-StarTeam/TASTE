# Experimenting / Experiment 模块

本目录是 TASTE 七阶段中的 `Experiment` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Modify or execute selected project code, run auditable experiments, parse metrics/logs, and produce evidence gates for claims.

在已锁定的 repo/data/env 上执行真实代码修改、实验启动、日志解析、指标登记、坏例分析和证据门控。它只消费当前执行合同和环境证据，不替代 Find/Plan 做选题。

## 输入

- selected_plan_contract / experiment_plan.json
- evidence_ready_repo_selection.json
- locked experiment_python / repo_path / dataset registry
- Claude Code 项目会话或明确命令模板

## 输出

- experiment_registry.json：实验记录和指标
- runs/artifacts/logs：命令、stdout、stderr、metrics、bad cases
- runtime integrity / reference reproduction audit
- 下一轮行动建议或 blocker

## 运行逻辑

1. 读取唯一执行计划和环境锁。
2. 由 Claude Code 或命令模板修改/运行实验。
3. 记录命令、PID、日志、指标和异常。
4. 用 watchdog/runtime audit 检查实验是否真的跑完、是否有证据。
5. 把可论文使用的结论交给 Writing 之前，必须通过复现/证据/坏例审计。

## 统一入口

- 公开入口：`/home/fmh/workspace/TASTE/modules/experimenting/main.py`。
- 框架调用格式：`python framework/scripts/run_module.py experimenting --action <action> ...`，或等价地直接调用 `python modules/experimenting/main.py --action <action> ...`。
- `scripts/` 下文件是模块私有后端实现，不应由网页前端直接拼路径调用；需要暴露时先在 `main.py` 注册 action。
- `cli.py` 仅为旧调用兼容层，必须保持薄转发。

## 文件结构

| 路径 | 作用 |
| --- | --- |
| `main.py` | 本模块唯一公开后端入口；框架和网页只能通过它指定 action 并传入显式输入。 |
| `cli.py` | 兼容入口，只转发到 `main.py`，不能承载业务逻辑。 |
| `contracts.py` | 声明模块外部输入、输入产物、输出产物和职责边界；供框架审计和独立运行说明使用。 |
| `script_manifest.json` | 当前脚本清单、函数、import 和归属原因；README 的脚本列表应和它保持一致。 |
| `scripts/` | 该模块真正的后端实现。新增脚本前应优先合并到下面列出的现有大块中。 |

## 脚本清单

### 主入口

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/run_loop.py` | 自动科研主循环入口，调用 init、run_project、trajectory supervisor；根日志写入 runtime/logs。 |
| `scripts/run_coding_agent.py` | 实验 Claude Code 项目代理入口。 |
| `scripts/launch_experiment_run.py` | 启动单个实验命令并建立 artifact/PID/log 合同。 |
| `scripts/log_experiment.py` | 登记实验结果、指标和 artifact。 |

### 运行与导入

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/experiment_contracts.py` | 实验 artifact 合同和结构化字段工具。 |
| `scripts/experiment_run_watchdog.py` | 监控实验进程、日志和超时。 |
| `scripts/import_experiment_artifacts.py` | 把外部/历史实验产物导入统一 registry。 |
| `scripts/run_active_repo_smoke.py` | 对当前 active repo 做快速 smoke。 |
| `scripts/run_real_repo_smoke.py` | 对真实数据 repo 做诚实短跑复现。 |

### 审计与分析

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/audit_experiment_iteration.py` | 检查 idea-code-run-log-analysis-reflection-next-plan 是否完整。 |
| `scripts/audit_experiment_runtime_integrity.py` | 根据 watchdog 和 artifact 合同重建运行完整性审计。 |
| `scripts/audit_reference_reproduction.py` | 审计参考工作是否已复现。 |
| `scripts/reference_reproduction_state.py` | 读取/归一化参考复现状态。 |
| `scripts/analyze_experiment_failures.py` | 分析实验失败原因和方法级失败模式。 |
| `scripts/build_experiment_record_table.py` | 生成用户可读实验记录表。 |

## 冗余控制原则

- Experiment 脚本可以向 experiment_runner.py、experiment_audits.py、experiment_records.py 三块收敛；但 launch/watchdog/log 三件事必须继续保持结构化合同。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
