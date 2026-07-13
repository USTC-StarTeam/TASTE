# Environment / 环境部署

Environment 根据一个已经选定的实验计划，让项目专属的 Environment 主控 Claude Code 完成仓库核验、Conda 环境部署、真实数据与 loader 准备、运行验证和参考复现，并输出可审计的阶段结论。

本模块只有一个公开入口：

```text
modules/environment/main.py
```

所有命令都必须使用 Conda 环境 `taste`。`scripts/` 是模块私有实现，用户、Web 和 Framework 都不应直接运行其中的文件。

## 使用前提

- 已创建 `projects/<project>/` 项目目录。
- 当前终端能运行 `conda`、`git` 和 `claude`。
- Claude Code 已由用户自行登录。
- 已有 JSON 格式的唯一选定实验计划。
- 仓库、数据集和模型需要联网时，当前机器具备相应访问权限。

Environment 不配置 Claude Code 账号，不保存 API key，不修改用户 shell 配置。

## 直接运行

最小命令：

```bash
conda run -n taste python modules/environment/main.py \
  --action deploy_from_plan \
  --project <project> \
  --plan /abs/path/to/experiment_plan.json
```

实验计划通常包含：

| 字段 | 用途 |
| --- | --- |
| `title`、`topic`、`objective` | 实验目标。 |
| `repo_url`、`repositories`、`repo_candidates` | GitHub 仓库候选。 |
| `dataset`、`datasets`、`data` | 数据集、下载或 loader 要求。 |
| `target_metrics`、`metrics` | 论文或计划要求的指标。 |
| `training`、`reproduction` | 训练、评估、checkpoint、硬件和复现说明。 |

常用参数：

| 参数 | 用途 |
| --- | --- |
| `--conda-env NAME` | 固定使用该实验 Conda 环境名。 |
| `--max-repair-rounds N` | 限制本次修复轮数。 |
| `--until-terminal` | 持续修复到终态或环境 handoff 就绪。 |
| `--max-total-rounds N` | 限制 `--until-terminal` 的总轮数。 |
| `--skip-full-reproduction` | 只处理环境、loader 和 smoke，不批准论文级复现。 |
| `--dry-run` | 验证输入、prompt 和产物结构，不调用 Claude 或执行部署命令。 |
| `--run-id LABEL` | 记录调用方标签；真实目录名仍由模块生成。 |

如果提供 `--conda-env`，Claude 的每轮计划都必须使用这个名称。如果没有提供，Environment 主控 Claude 在第一轮选择一个名称，模块立即固定，后续轮次和新 run 命令继续使用它。

## Environment 模块主控会话

每个项目只有一个 Environment 主控 Claude 会话。模块在首次真实调用前为项目固定会话 ID，之后的部署、修复、审计和网页对话都只恢复这个会话。

Claude 进程的工作目录始终是：

```text
projects/<project>/
```

模块 run 和 Environment skill 仅作为该会话的附加可读写目录。项目与会话的对应关系保存在模块本地 `.runtime/controllers/<project>/`，由 Environment 自己维护。

## 网页对话

网页的 Environment 对话框直接向该项目的 Environment 主控 Claude 发送消息。等价命令是：

```bash
conda run -n taste python framework/scripts/run_module.py environment \
  --action chat \
  --project <project> \
  --message "请检查当前环境门控并完成下一项环境工作。"
```

主控空闲时会立即处理。主控忙碌时：

- 普通发送进入 Environment 模块队列，网页显示正在排队的消息。
- “打断当前任务并优先发送”会终止当前 Claude turn，优先处理该网页指令。
- 网页指令完成后，同一会话继续被打断的 Environment 工作。
- 多条消息按优先级和入队时间串行执行，不会为同一项目启动第二个 Environment 会话。
- 排队没有等待超时；消息只会完成或被用户明确取消。
- 网页保留并显示该项目同一 Environment 会话的历史指令与回复。

直接调用抢占：

```bash
conda run -n taste python framework/scripts/run_module.py environment \
  --action chat \
  --project <project> \
  --message "立即处理这条环境指令。" \
  --interrupt-current
```

## Conda 名称

网页“环境配置”中保存的 Conda 环境名优先级最高，Framework 会原样传给 Environment。网页未填写时，Claude 选择的合法名称会写入本次 run 的 `conda_environment.json`，Framework 将该名称固定到项目配置；handoff 通过后再固定对应 prefix 和 Python。

名称和路径是两个字段：

- `conda_env`：用户或 Claude 固定的环境名。
- `conda_env_prefix`：本次 Environment run 中的实际 Conda 前缀路径。

## Run 与产物

每次部署或对话开始时，`main.py` 立即创建唯一目录：

```text
modules/environment/.runtime/runs/<YYYYMMDDTHHMMSSffffffZ_action_pidPID>/
```

目录名包含 UTC 微秒时间、action 和 PID。进程启动后只能使用这个目录。不同进程不会共享或改用其他 run。

完成后只更新一个人工审查符号链接：

```text
modules/environment/.runtime/latest_run -> runs/<本次完成的 run>
```

模块不会复制 run。程序必须使用明确的 `.runtime/runs/<run_id>/`，不得使用 `latest_run`。

模块按项目和 action 保留最近 5 个完成 run。项目 `environment_handoff.json`、`environment_latest_run.json` 或 `latest_run` 人工审查链接引用的 run 不会被自动清理。

主要产物：

| 文件 | 说明 |
| --- | --- |
| `run_meta.json` | action、project、PID、创建和完成时间、返回码。 |
| `input_plan.raw.json`、`input_plan.normalized.json` | 原始和规范化实验计划。 |
| `machine_profile.json` | 当前机器的 Conda、GPU、CUDA 和工具信息。 |
| `repo_info.json`、`repo_evidence.json` | 仓库来源、commit、文档和入口证据。 |
| `paper_evidence.json` | 论文、计划和目标指标证据。 |
| `conda_environment.json` | 已固定的 Conda 环境名及来源。 |
| `round_*/claude_environment_plan_round_*.json` | Claude 每轮环境计划。 |
| `round_*/command_receipts.json` | 受控命令的真实回执。 |
| `environment_deployment_decision.json` | 最终裁决与 Environment handoff。 |
| `environment_chat_result.json` | 一次网页/命令行对话的排队、抢占和回复结果。 |

查看最近完成的部署裁决：

```bash
conda run -n taste python modules/environment/main.py --action status --project <project>
```

输出顶层 `status` 表示 handoff 是否就绪；`read_status` 只表示裁决文件是否成功读取。

查看指定 run：

```bash
conda run -n taste python modules/environment/main.py \
  --action status \
  --project <project> \
  --run-id <run_dir_name>
```

## 裁决

| 结果 | 含义 |
| --- | --- |
| `environment_handoff.ready_for_experimenting=true` | 仓库、run-local Conda、真实数据/loader/model smoke、必需命令和工作区审计已通过，可以交给 Experimenting。 |
| `decision=approve` | 参考复现、论文配置和指标证据达到批准门槛。 |
| `decision=continue_repair` | 仍存在可修复的依赖、数据、路径、配置、命令或指标问题。 |
| `decision=reject` | 已有证据证明仓库、论文、数据权限或本机算力存在不可修阻断。 |

安装成功或 smoke 成功不等于论文级复现。Environment 可以先完成 handoff，再由 Experimenting 继续论文级实验。

## Framework 与 Web

- Web 只保存配置、发送命令、显示排队状态和读取项目投影。
- Environment 部署按钮启动一次完整的 Framework Environment 单阶段流程，不自动降级为 smoke 或单轮修复。
- Framework 只执行 `modules/environment/main.py`，并在模块结束后读取明确的 run 输出。
- Framework 不复制 Environment run，只写轻量项目状态投影。
- Environment 不导入或调用 Framework；直接运行与 Framework 运行使用同一公开接口。

常见项目投影：

```text
projects/<project>/state/environment_handoff.json
projects/<project>/state/environment_latest_run.json
projects/<project>/state/environment_controller_last_result.json
projects/<project>/state/environment_chat_latest.json
projects/<project>/reports/environment_controller.md
```

`evidence_ready_repo_selection.json` 和 `active_repo.json` 只在 handoff gate 通过后更新。
