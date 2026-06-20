# Environment / 环境部署模块

`modules/environment` 是 TASTE 七阶段中的正式环境阶段后端模块。它给定实验 plan，在模块私有运行目录里让 Claude Code 审计候选 GitHub 仓库、部署 Conda 环境、准备真实数据/loader、运行参考复现，并给出 `approve`、`reject` 或 `continue_repair` 裁决。

本模块不依赖 Web 前端，不直接写项目产物，也不修改其它模块。Web 或 framework 调用时，只能通过 `modules/environment/main.py` 公开入口传入 plan 和运行参数。

## 运行环境

```bash
ssh hidimension_5090_1
cd /home/fmh/workspace/TASTE
source /home/fmh/workspace/miniforge/etc/profile.d/conda.sh
conda activate ar_taste
source /home/fmh/workspace/.nvm/nvm.sh
```

`python` 与 `rg` 必须来自 `ar_taste`。Claude Code 使用远程 nvm。

## 输入口径

最小输入是一个 JSON 实验 plan：

```bash
python modules/environment/main.py \
  --action deploy_from_plan \
  --plan /abs/path/to/experiment_plan.json \
  --run-id demo_environment
```

plan 可以包含：

- `title/topic/objective`：研究任务。
- `repo_url/github_url/repositories/repo_candidates`：候选 GitHub 仓库。
- `dataset/datasets/data`：真实数据集或数据要求。
- `target_metrics/metrics`：论文或 plan 要复现的指标。
- `training/reproduction`：训练、评估、checkpoint、硬件等要求。

公开 action：

| action | 作用 |
| --- | --- |
| `deploy_from_plan` / `run` / `deploy` | 主流程：仓库候选、环境计划、命令执行、参考复现、最终裁决。 |
| `status` | 读取模块最近裁决和状态。 |
| `--contract` | 输出模块契约。 |

## 输出口径

所有运行产物限制在：

```text
modules/environment/runs/<run_id>/
```

关键文件：

| 文件 | 作用 |
| --- | --- |
| `environment_deployment_decision.json` | 最终裁决；包含 `decision`、`allow_next_module`、approval gate、workspace audit。 |
| `claude_environment_plan_round_*.json` | Claude 生成的环境部署计划。 |
| `command_receipts_round_*.json` | 每轮命令执行回执。 |
| `.runtime/`、`conda_envs/`、repo/data/log 子目录 | 本 run 的隔离运行环境、中间文件和日志。 |
| `modules/environment/latest_decision.json` | 最近一次裁决索引，供调试查看。 |

批准进入实验必须通过仓库来源、仓库文档、Conda 环境、本机资源、真实数据、必要命令、论文 claim、指标证据、完整复现和工作区审计等 gate。没有真实完整复现时，正确结果是 `continue_repair`，不能伪造 approve。

### PyTorch / PyG / CUDA 依赖策略

在 RTX 5090、compute capability 12.x 或 Claude 计划使用 PyG 的场景下，后端会在执行前规范化明显不可解算的依赖计划：

- 不接受 `conda -c pyg pyg pytorch-scatter pytorch-sparse pytorch-cluster` 作为 PyTorch >= 2.5 / CUDA 12.x 的安装路线。该组合在当前 conda channel 上容易被求解到 CPU PyTorch 或互斥的 Python/PyTorch/CUDA 矩阵。
- 对 PyG 工作负载，后端使用 Python 3.11、PyTorch CUDA 12.8 pip wheel，以及与 torch 版本匹配的 `https://data.pyg.org/whl/torch-<version>+cu128.html` 官方 PyG wheel index。
- 后端会补充 `verify_pyg_cuda_import` 必需验证，要求 `torch.cuda.is_available()` 为真，并能导入 `torch_geometric`、`torch_scatter`、`torch_sparse`、`torch_cluster`。
- 所有策略改写会写入环境计划的 `plan_policy_rewrites` 与 `backend_dependency_policy`，保留原始 Claude 计划和后端改写原因，便于审计。


## 运行流程

1. 读取并规范化实验 plan，抽取标题、主题、仓库候选、数据和指标。
2. 如果 plan 给出候选仓库，Claude 只在候选内排序；如果没有候选，Claude 可尝试发现可信官方 GitHub，不能编造。
3. 克隆或复用仓库到本 run 目录，收集 README、配置、入口、数据和论文证据。
4. 探测本机 GPU/CUDA/Conda 画像，生成本机适配的环境部署计划。
5. 受控执行 Conda、安装、数据、verify、smoke、`reproduce_full` 等命令。
6. 解析指标和回执，重算 approval gate。
7. 写出 `approve/reject/continue_repair` 裁决；运行期间若写出模块外路径，会被 workspace audit 阻断。

## 与 framework/web 的关系

- 单独调用本模块时，产物只在 `modules/environment` 内，不会覆盖 `projects/<project>` 的网页产物。
- Web 环境按钮由 `web -> framework -> environment` 调用：web 只传项目 plan/venue/config，framework 记录状态，environment 做模块内裁决。
- 项目 Claude Code 可直接调用本模块辅助科研，但这类 standalone 产物不会自动变成网页显示的项目产物。

## 脚本结构

| 目录 | 作用 |
| --- | --- |
| `scripts/orchestration/` | 主编排入口，包含 `autonomous_deploy.py` 和状态读取。 |
| `scripts/common/` | JSON、路径、shell、安全命令、Claude runner、plan schema 等公共工具。 |
| `scripts/repository/` | 仓库克隆、复用、证据收集。 |
| `scripts/environment/` | 本机画像、Conda/runtime 探测。 |
| `scripts/reproduction/` | 论文证据、指标比较、裁决和 gate 重算。 |

## 维护原则

- 不新增绕过 `main.py` 的公开入口。
- 不把旧项目状态、旧 active_repo 或弱 fallback 当作当前主线证据。
- 可修复问题优先继续修复；只有不可修且证据充分时才 reject。
- 所有人类可读说明使用中文，运行产物不进入 git。
