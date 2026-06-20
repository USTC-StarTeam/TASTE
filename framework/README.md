# TASTE framework 后端框架

`framework/` 是 TASTE 的正式后端编排层。它不实现七个科研模块的内部算法，只负责读取七模块公开契约、决定调用顺序、执行模块公开入口、记录命令回执，并把框架状态整理成 Web 可读的 JSON/Markdown。

七个模块是：`finding`、`reading`、`ideation`、`planning`、`environment`、`experimenting`、`writing`。框架只通过 `modules/<stage>/main.py --contract` 和 `modules/<stage>/main.py --action ...` 调用它们。

## 运行环境

必须在远程工作区运行：

```bash
ssh hidimension_5090_1
cd /home/fmh/workspace/TASTE
source /home/fmh/workspace/miniforge/etc/profile.d/conda.sh
conda activate ar_taste
source /home/fmh/workspace/.nvm/nvm.sh
```

`python`、`rg` 来自 `ar_taste`；Claude/Node 使用远程 nvm。不要使用系统 Python 或本地机器路径。

## 输入口径

框架输入是“科研目标 + 项目信息 + 模块公开参数”，不是前端页面对象，也不是模块私有中间文件。常用参数：

- `--research-goal`：自然语言研究目标。
- `--project`：项目 ID，仅作为框架状态字段；是否传给模块由 `--module-arg` 决定。
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

模块科学产物仍由各模块写在自己的目录；框架状态不会直接覆盖项目产物。Web 需要展示时，由 Web 后端读取框架 public 状态和项目目录里的正式产物做投影。

## 七阶段默认动作

| 阶段 | 默认动作 | 职责 |
| --- | --- | --- |
| finding | `find` | 召回、过滤、评分、排序候选文献/工具。 |
| reading | `read` | 获取全文证据并生成精读材料。 |
| ideation | `run` | 从文献证据生成候选研究想法。 |
| planning | `plan` | 形成可执行实验计划和唯一执行合同。 |
| environment | `deploy_from_plan` | 根据实验 plan 选择/部署仓库、数据、环境并裁决能否进入实验。 |
| experimenting | `run` | 在锁定 repo/env 中执行实验并维护实验记录。 |
| writing | `run` | 基于证据生成、修订、编译和审计论文。 |

## 单独使用

查看契约：

```bash
python framework/scripts/orchestration/run_taste_framework.py contracts --python "$(which python)"
```

完整七阶段 dry-run：

```bash
python framework/scripts/orchestration/run_taste_framework.py run \
  --mode dry-run \
  --strategy deterministic \
  --research-goal "验证七模块编排" \
  --run-id demo_dry_run \
  --python "$(which python)"
```

Web 单阶段链路示例：

```bash
python framework/scripts/orchestration/run_taste_framework.py run \
  --mode execute \
  --only-stage environment \
  --project protein \
  --venue ICLR \
  --plan-json projects/protein/state/experiment_plan.json \
  --module-arg "environment=--plan projects/protein/state/experiment_plan.json --run-id web_environment_protein"
```

查看状态：

```bash
python framework/scripts/orchestration/run_taste_framework.py status --run-id demo_dry_run
```

从已有 environment run 刷新并同步 handoff 到项目状态（不会启动新环境部署或实验，只读取 `modules/environment/runs/<run_id>` 里的真实回执重新计算 `environment_handoff.ready_for_experimenting`）：

```bash
python framework/scripts/orchestration/run_taste_framework.py sync-environment-handoff \
  --project protein \
  --environment-run-dir modules/environment/runs/<run_id>
```

该命令写入 `projects/<project>/state/environment_handoff.json`、`evidence_ready_repo_selection.json` 和 `active_repo.json`。`ready_for_experimenting=true` 只表示 repo、run-local Conda、数据准备和 loader/model smoke 可交给 experimenting；`allow_next_module=true` 仍必须等待真实 full reproduction 和论文指标证据。

## 维护原则

- 框架只做编排、传参、状态记录和门控串联，不把模块实现搬进框架。
- 单模块产物留在模块目录；项目产物由框架/Web/项目代理按规则同步或投影。
- Web 单阶段按钮必须走 `web -> framework -> module`，不能绕开新契约调用旧脚本。
- 新增兼容逻辑应进统一路径解析/PYTHONPATH/契约层，不在业务分支硬塞路径。
- `tests/` 只保留能守住七模块契约和 Web 桥接的核心测试。
