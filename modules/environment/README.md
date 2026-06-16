# Environment / 环境模块

本目录是 TASTE 七阶段中的 `Environment` 独立模块边界。TASTE 框架可以通过网页和任务队列调用它，但模块自身必须只依赖显式输入和外部维护的产物，不能隐式读取其它阶段的历史状态来替代输入。

## 职责边界

契约原文：Select audited code/data bases, probe loaders, and lock the experiment runtime. It does not run novel experiments or write paper claims.

根据计划合同选择和验证代码/数据基底，检查 loader、真实数据、参考复现和 Conda/Python 运行环境，锁定实验运行条件。它不生成 idea，不运行创新实验，不写论文结论。

## 输入

- selected_plan_contract / experiment_plan.json
- Find/Plan 里提到的 repo/data 候选
- runtime 配置：conda base、实验 python、下载工具等
- 项目已有 repos/datasets/artifacts

## 输出

- evidence_ready_repo_selection.json：可执行基底选择
- repo_env_bootstrap.json：环境部署记录
- dataset registry / data requirements / acquisition plan
- reference reproduction / base switch gate / viability audit

## 运行逻辑

1. 读取计划合同和候选 repo/data。
2. 审计当前候选是否有真实代码、真实数据和 loader。
3. 必要时尝试官方/公开数据下载，但必须记录证据和失败原因。
4. 只有确定性 base-switch gate 通过时，才允许替换当前基底。
5. 环境锁定后 Experiment 复用，不反复创建或随意切换。

## 统一入口

- 公开入口：`/home/fmh/workspace/TASTE/modules/environment/main.py`。
- 框架调用格式：`python framework/scripts/run_module.py environment --action <action> ...`，或等价地直接调用 `python modules/environment/main.py --action <action> ...`。
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

### 主入口和选择

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/run_environment_stage.py` | 网页 Environment 主入口，串联 repo/data/env 检查和诚实 gate。 |
| `scripts/select_evidence_ready_repo.py` | 选择同时有代码和真实数据证据的 repo。 |
| `scripts/select_fresh_research_base.py` | 准备当前 Find 的新基底候选池，不直接越权替换执行基底。 |
| `scripts/select_repo_candidate.py` | 轻量候选选择入口。 |

### 环境与数据准备

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/bootstrap_repo_env.py` | 创建/检查 Conda 环境、pip 安装、导入验证和运行命令记录。 |
| `scripts/build_repo_data_requirements.py` | 从仓库和计划中构建数据需求合同。 |
| `scripts/plan_data_acquisition.py` | 生成数据获取计划。 |
| `scripts/attempt_data_acquisition.py` | 按公开来源尝试下载/获取数据并记录证据。 |
| `scripts/probe_repo_dataset.py` | 探测 repo 数据集和 loader；没有通用适配时给出安全 blocker。 |
| `scripts/probe_fresh_base_data_acquisition.py` | 对当前 Find 新基底做有界数据获取探针。 |
| `scripts/register_dataset.py` | 登记数据集可用性和 readiness。 |
| `scripts/register_repo_candidate.py` | 登记 repo 候选。 |
| `scripts/repo_first_backtrack.py` | 从 repo 反向推断数据需求和缺口。 |
| `scripts/restart_after_data_blocker.py` | 数据阻塞后扩大候选发现，而不是硬塞当前不可用基底。 |

### 审计与 gate

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/assess_repo_candidates.py` | 评估候选 repo。 |
| `scripts/audit_local_repo.py` | 本地 repo 结构和信号审计。 |
| `scripts/audit_dataset_path.py` | 检查数据路径是否真实存在且可读。 |
| `scripts/audit_repo_candidate_pool.py` | 深度审计候选池，防止错误切换。 |
| `scripts/audit_selected_base_viability.py` | 判断当前选中基底是否仍可继续修复或应进入切换 gate。 |
| `scripts/audit_deterministic_base_switch_gate.py` | 确定性审计 base switch 是否被授权。 |
| `scripts/execute_authorized_base_switch.py` | 只有 gate 通过后才执行基底切换。 |
| `scripts/guard_selected_base_route.py` | 保护当前基底路线不被历史/控制路线覆盖。 |
| `scripts/audit_obsolete_baseline_cleanup.py` | 生成旧基底清理计划，不直接删除文件。 |
| `scripts/reconcile_active_and_pool_candidates.py` | 区分 active repo 证据和探索候选池证据。 |

### 参考复现

| 脚本 | 真实作用 |
| --- | --- |
| `scripts/probe_candidate_base_reference.py` | 采集候选基底参考复现证据。 |
| `scripts/probe_selected_base_reference.py` | 对当前选中基底触发参考证据探测。 |
| `scripts/run_selected_base_reference_reproduction_audit.py` | 包装参考复现命令、解析指标并写入审计。 |
| `scripts/run_safe_unblock.py` | 针对当前新基底的安全解阻循环。 |
| `scripts/data_unavailability_policy.py` | 数据不可用时的诚实策略和替代候选判断。 |
| `scripts/build_fresh_base_implementation_plan.py` | 在新基底已获证据后生成实现计划。 |

## 冗余控制原则

- Environment 仍是脚本最多的模块。后续应优先合并为 repo_selection.py、data_contracts.py、base_switch_gates.py、environment_bootstrap.py、reference_reproduction.py 五个大块；保留当前入口名时可用薄 wrapper 兼容。
- 任何合并都必须保持 base-switch gate、数据证据和当前路线保护，不允许为了文件数减少破坏安全边界。
- 修改本模块时必须先读相关脚本和 manifest，找到根因后再改；禁止为某个论文、某个项目、某个本机路径写特异规则。
- 用户可见产物必须一遍生成正确；fallback 只能作为最后兼容路线，不能替代主流程质量。
