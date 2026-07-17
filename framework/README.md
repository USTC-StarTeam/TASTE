# TASTE framework 后端框架

`framework/` 是 TASTE 的正式后端编排层。它不实现七个科研模块的内部算法，只负责读取七模块公开契约、决定调用顺序、执行模块公开入口、记录命令回执，并把框架状态整理成 Web 可读的 JSON/Markdown。

七个模块是：`finding`、`reading`、`ideation`、`planning`、`environment`、`experimenting`、`writing`。框架只通过 `modules/<stage>/main.py --contract` 和 `modules/<stage>/main.py --action ...` 调用它们。

## 运行环境

必须在 TASTE 工作区根目录和 conda `taste` 环境运行：

```bash
cd <TASTE_ROOT>
conda activate taste
```

不要在 tracked 配置中写固定 Conda、Node、GPU 或工作区绝对路径。Claude Code 和 Node 使用当前用户自己的安装与账号配置。

## 输入口径

框架输入是“科研目标 + 项目信息 + 模块公开参数”，不是前端页面对象，也不是模块私有中间文件。常用参数：

- `--research-goal`：自然语言研究目标。
- `--project`：项目 ID。Environment 的部署与对话会由 Framework 自动传给模块公开入口。
- `--venue`：目标会议/期刊。
- `--mode dry-run|execute`：`dry-run` 只写命令计划，`execute` 才实际运行模块。
- `--strategy deterministic|hybrid|claude`：确定性七阶段推进，或交给 Claude Code 决策。
- `--plan-json`：可选 JSON，支持 `research_goal/project/venue/module_args`。
- `--module-arg stage=...`：给指定模块追加公开 CLI 参数。
- `--only-stage stage`：只运行一个或几个阶段，供 Web 的单阶段按钮走 `web -> framework -> module` 链路。

## 输出口径

框架自身状态只写入：

```text
framework/workspace/runs/<run_id>/
```

关键文件：

| 文件 | 作用 |
| --- | --- |
| `state/workflow_state.json` | 框架完整状态、记录、阻塞和下一步。 |
| `public/frontend_status.json` | Web 可读状态，包含进度、当前阶段、阻塞、最近回执。 |
| `public/workflow_status.md` | 人类可读状态页。 |
| `public/module_contracts*.json` | 本次使用的七模块契约。 |
| `logs/*.stdout.log` / `logs/*.stderr.log` / `logs/*.receipt.json` | 每条模块/门控命令的日志和回执。 |

模块科学产物先写入各模块自己的 timestamp run；Framework 校验明确 run 后生成 Web 所需的项目状态投影。Environment run 不复制到项目，Framework 只写 handoff、Conda 名称/前缀和完整对话历史等轻量状态。Web 只读取这些投影和项目正式产物。Ideation 的完整候选只存在于 `idea.md` 和派生的 `ideas.json`，current-Find state 不保存第二份 idea 列表。

## 七阶段默认动作

| 阶段 | 默认动作 | 职责 |
| --- | --- | --- |
| finding | `find` | 召回、过滤、评分、排序候选文献/工具。 |
| reading | `read` | 获取全文证据并生成精读材料。 |
| ideation | `run` | 从文献证据生成候选研究想法。 |
| planning | `plan` | 从一个或多个选中的已批准 Ideas 形成候选计划。 |
| environment | `deploy_from_plan` | 根据实验 plan 选择/部署仓库、数据、环境并裁决能否进入实验。 |
| experimenting | `work` | 把项目交给该项目唯一的 Experimenting 主控会话；会话在项目目录中执行、验证并维护实验记录。 |
| writing | `run` | 基于证据生成、修订、编译和审计论文。 |

## 单独使用

查看契约：

```bash
python framework/scripts/main.py workflow contracts --python "$(which python)"
```

完整七阶段 dry-run：

```bash
python framework/scripts/main.py workflow run \
  --mode dry-run \
  --strategy deterministic \
  --research-goal "验证七模块编排" \
  --run-id demo_dry_run \
  --python "$(which python)"
```

Web 单阶段链路示例：

```bash
python framework/scripts/main.py workflow run \
  --mode execute \
  --only-stage environment \
  --project protein \
  --venue ICLR \
  --plan-json projects/protein/state/experiment_plan.json \
  --module-arg "environment=--plan projects/protein/state/experiment_plan.json --run-id web_environment_protein"
```

当前 Find 的 Reading 由 Framework 读取推荐项、保留可用的 URL/PDF/DOI/OpenReview 等定位信息，并调用 Reading 的通用 `read` action：

```bash
conda run -n taste python framework/scripts/main.py module reading \
  --action current_find_research_plan \
  --project <project>
```

Reading 只接收生成在自身 timestamp run 中的通用输入 JSON；项目校验和产物同步由 Framework 完成。

当前 Find 的 Ideation 也由专用桥接处理：Framework 校验项目、当前 Find、Read 完成标记和 Read validation，提取去重证据并构建单个输入包，再调用 `modules/ideation/main.py`。模块返回后，Framework 只接受身份一致的明确 timestamp run，并同步到项目；生成、卡片编辑和整篇 Markdown 编辑共用同一个项目级阻塞锁：

```bash
conda run -n taste python framework/scripts/main.py module ideation \
  --action idea \
  --project <project> \
  --run-id <current_find_run_id>
```

Web 不读取 Find/Read 来构造 Ideation 输入，也不访问模块 `.runtime`。`latest_run` 只是模块的人工作业副本，Framework 不使用它。

当前 Find 的 Planning 由专用桥接处理：Framework 校验 Read/Ideas、run_id 和选中的已批准 Idea ID，生成显式输入包，调用 `modules/planning/main.py`，再校验并同步模块返回的精确 timestamp run。可选一个或多个 `--idea-id`；不传时使用全部已批准项。生成候选与选择执行计划是两个动作：

```bash
conda run -n taste python framework/scripts/main.py module planning --action plan --project <project>
conda run -n taste python framework/scripts/main.py module planning --action select --project <project>
```

Web 不准备 Planning 输入、不判断项目就绪状态，也不复制 Planning 产物。

如果 Ideation 已同步的修改影响当前 `plans.json` 中的 Idea，Framework 会使当前 `plan.md` 和机器投影失效，并用原已规划且仍批准的 Idea 自动重跑 Planning，默认精确修复 3 轮；`planning_runs/` 历史不参与当前状态，也不会被删除。Full Cycle 在 Plan 成功后自动执行 `current-find-selection`，取得唯一执行计划后才进入 Environment。

查看状态：

```bash
python framework/scripts/main.py workflow status --run-id demo_dry_run
```

从已有 environment run 刷新并同步 handoff 到项目状态（不会启动新环境部署或实验，只读取 `modules/environment/.runtime/runs/<run_id>` 里的真实回执重新计算 `environment_handoff.ready_for_experimenting`）：

```bash
python framework/scripts/main.py workflow sync-environment-handoff \
  --project protein \
  --environment-run-dir modules/environment/.runtime/runs/<run_id>
```

该命令写入 `projects/<project>/state/environment_handoff.json`、`evidence_ready_repo_selection.json` 和 `active_repo.json`。`ready_for_experimenting=true` 只表示 repo、run-local Conda、数据准备和 loader/model smoke 可交给 experimenting；`allow_next_module=true` 仍必须等待真实 full reproduction 和论文指标证据。

Environment 对话始终通过同一个公开入口进入模块自己的项目唯一会话：

```bash
conda run -n taste python framework/scripts/main.py module environment \
  --action chat --project <project> --message "<instruction>"
```

普通消息在主控忙碌时持续排队且没有等待超时；追加 `--interrupt-current` 会优先处理本条消息，并在完成后恢复被打断的 Environment 工作。取消对应 Web job 会撤销尚未处理的消息并终止该消息正在使用的 Claude turn。

## 维护原则

- 框架只做编排、传参、状态记录和门控串联，不把模块实现搬进框架。
- 单模块产物留在模块目录；项目产物只由 Framework 按明确 run 同步，Web 不复制或改写模块产物。
- Web 单阶段按钮必须走 `web -> framework -> module`，不能绕开新契约调用旧脚本。
- 新增兼容逻辑应进统一路径解析/PYTHONPATH/契约层，不在业务分支硬塞路径。
- `tests/` 只保留能守住七模块契约和 Web 桥接的核心测试。
