# Experimenting / 实验执行模块

`modules/experimenting` 是 TASTE 七阶段中的正式实验阶段后端模块。它在给定实验 plan、已锁定代码仓库和 Conda 环境后，让 Claude Code 在目标 repo 内做最小、可审计的实验迭代，并维护实验记录、指标和日志。

本模块不负责选择仓库或搭环境；这些属于 `environment`。本模块也不写论文；论文属于 `writing`。

## 运行环境

```bash
ssh hidimension_5090_1
cd /home/fmh/workspace/TASTE
source /home/fmh/workspace/miniforge/etc/profile.d/conda.sh
conda activate ar_taste
source /home/fmh/workspace/.nvm/nvm.sh
```

`python` 和 `rg` 来自 `ar_taste`；实验命令通过指定的 `--conda-env` 运行。

## 输入口径

最小输入：

```bash
python modules/experimenting/main.py \
  --action autonomous_experiment \
  --plan /abs/path/to/experiment_plan.json \
  --repo-path /abs/path/to/selected/repo \
  --conda-env /abs/path/to/environment/run/conda_envs/experiment_env \
  --output-root modules/experimenting/runtime/autonomous \
  --max-iterations 3
```

参数说明：

| 参数 | 作用 |
| --- | --- |
| `--plan` | JSON/YAML/文本实验计划。 |
| `--repo-path` | environment 已锁定的基础代码仓库；Claude 默认只能修改这个 repo。 |
| `--conda-env` | 实验运行 Conda 环境名或绝对 Conda prefix；framework/web 通常传 `projects/<project>/state/environment_handoff.json` 中的 `conda_env_prefix`。 |
| `--output-root` | 模块状态、记录、日志输出根；默认在 `modules/experimenting/runtime/autonomous`。 |
| `--max-iterations` | 最大实验迭代轮数。 |
| `--skip-claude` | 只做环境/记录流程自测，不调用 Claude；不能作为科研成功证据。 |
| `--permission-mode` | Claude Code 权限模式；无人值守 web/framework 链路默认 `bypassPermissions`，避免 Bash/Python 命令等待人工批准。 |
| `--dry-run` | 只写计划和环境锁，不运行 Claude 或验证命令。 |

从 Web 正常进入实验阶段时，输入由 `web -> framework -> environment_handoff -> experimenting` 传递：`repo_path` 指向 environment run 内的真实仓库，`conda_env_prefix` 指向同一 run 内已验证的 Conda 环境，`pending_downstream_metrics` 是本阶段需要通过真实实验/评估日志绑定的论文指标。

公开 action：

| action | 作用 |
| --- | --- |
| `autonomous_experiment` / `run` | 主实验迭代流程。 |
| `runtime_env` | 运行环境检查/锁定辅助。 |
| `coding_agent` | 直接调用实验 Claude agent。 |
| `launch` | 执行一次验证/训练命令。 |
| `reference_reproduction`、`audit_iteration`、`runtime_integrity` | 质量门控/审计动作。 |
| `--contract` | 输出模块契约。 |

## 输出口径

默认输出在：

```text
modules/experimenting/runtime/autonomous/
```

Web/framework 单阶段调用会把 `--output-root` 指到：

```text
modules/experimenting/runtime/web/<project>/
```

关键产物：

| 文件/目录 | 作用 |
| --- | --- |
| `environment_lock.json` | Conda/NVM/Claude/runtime 锁定信息。 |
| `state/experiment_registry.json` | 结构化实验记录。 |
| `experiment_records.csv` | 表格化实验记录。 |
| `实验记录.md` | 人类可读实验记录。 |
| `runs/<run_id>/iteration_*/` | 每轮 Claude prompt、stdout、validation stdout、metrics、bad cases、summary。 |
| `runs/<run_id>/iteration_*/wrapper_iteration_result.json` | 包装器验收结果，包含 `acceptance_status`、`acceptance_blockers`、Claude 权限拒绝和 summary 路径。 |

模块独立运行时，这些产物不会自动覆盖 `projects/<project>` 里的网页产物。framework/web 只读取或投影必要状态；项目 Claude Code 可使用这些产物辅助项目内工作。

## 运行流程

1. 解析实验 plan，归一化 experiment_id、标题、方法、数据、指标、运行命令和 Conda 环境。
2. 检查 `repo-path` 和 Conda/NVM/Claude runtime，支持 Conda 环境名或绝对 prefix，并写出环境锁。
3. 每轮构建 Claude Code prompt，要求它只修改基础 repo，只把本轮日志/指标写入 iteration artifact dir。
4. 可选执行 plan 或 `--run-command` 中的验证命令。
5. 收集 `metrics.json`、`bad_cases.json`、`experiment_iteration_summary.json`，更新 registry/CSV/Markdown。
6. 执行包装器验收：Claude 返回 0 只是必要条件；若出现当前结构化 `permission_denials`、缺少 `experiment_iteration_summary.json`、summary 状态为 blocked/failed/error、summary 自报非 accepted 的 `acceptance_status` 或非空 `acceptance_blockers`、没有真实命令/产物/指标/日志证据，则本轮记为 failed，并写出 `acceptance_blockers`。
7. 只有验收通过才提前停止；失败时保留失败原因和下一步建议。

## 脚本结构

| 目录 | 作用 |
| --- | --- |
| `scripts/orchestration/` | 主实验循环与框架循环。 |
| `scripts/common/` | plan schema、runtime lock、记录表、文件工具。 |
| `scripts/agent/` | Claude Code 实验代理。 |
| `scripts/execution/` | 启动命令、smoke、watchdog。 |
| `scripts/audits/` | 参考复现、迭代审计、runtime 完整性审计。 |
| `scripts/analysis/` | 失败分析。 |
| `scripts/records/` | 实验记录导入和表格工具。 |

## 维护原则

- 实验代码修改只发生在 `--repo-path` 指向的基础 repo。
- 模块状态和中间产物只写 `modules/experimenting/runtime` 或调用方显式传入的模块内输出根。
- 不把失败实验包装成成功指标；没有真实日志、命令/产物证据或指标就保持 blocked/failed。
- 不把论文写作、仓库选择、环境部署逻辑塞进本模块。
