# Planning / Plan 模块

本目录是 TASTE 七阶段中的 `Planning` 独立后端模块。它只负责把已经通过或显式输入的 idea 变成可审计、可执行、可交给后续 Claude Code 的研究计划和执行合同；它不属于网页前端，也不需要前端才能运行。

职责原句：Select and repair executable research plans from approved ideas; downstream modules consume only explicit selected plan contracts.

## 1. 职责边界

Planning 做三件事：

1. 读取 idea：可以来自当前 Find/Read/Idea 产物，也可以来自 standalone 的 `--idea-json`、`--idea-md` 或 `--idea-text`。
2. 生成/修复 plan：用 LLM/Claude Code 风格的计划生成与评估提示，反复补齐方法机制、环境证据要求、基线、消融、指标、失败分析、go/no-go 和论文证据门槛。
3. 写出执行合同：生成 `plans.json`、`plan.md`、`experiment_plan.json`、`taste_plan_bridge.json`，让后续 Environment/Experiment/Writing Claude Code 只消费显式选中的 `selected_plan_id`。

Planning 不能做这些事：

- 不能替 Environment 选择具体 base paper、repo、本地路径、数据路径或训练命令。
- 不能因为只有一个候选 plan 就自动授权下游执行；必须由 Claude Code/人类显式选择，或通过 `finish_plan` 标记唯一计划。
- 不能读写前端实现，也不能把中间运行产物放到本目录外的临时位置。

## 2. 输入口径

### 框架/项目调用

- `run_id`：已有 TASTE run 的编号。
- `ideas.json`：包含 `ideas` 列表，idea 至少应有 `id/idea_id/title` 与 `new_method/hypothesis/method_details/initial_experiment` 中的有效内容。
- 可选 `--idea-id`：只为指定 idea 生成 plan。
- 可选 `--config-json`：LLM 配置，结构沿用 `auto_research.models.AppConfig`。

### 单独运行调用

- `--idea-json`：一个 idea 对象、idea 对象列表，或 `{"ideas": [...]}`。
- `--idea-md`：一个 Markdown 文件，第一行标题会作为 idea 标题，全文作为方法描述。
- `--idea-text`：命令行直接传入的 idea 文本。
- `--output-dir`：standalone 输出目录；默认是 `modules/planning/runs/<run-id>`，已被 `.gitignore` 排除；显式传入时也必须位于 `modules/planning` 内，否则会拒绝运行。

## 3. 输出口径

| 文件 | 作用 |
| --- | --- |
| `plans.json` | 所有候选计划、版本、评估/修复记录、显式选择状态。 |
| `plan.md` | 给人类审阅和修改的计划正文。 |
| `experiment_plan.json` | 给后续 Claude Code 的机器可读执行合同；没有唯一选中计划时状态为 blocked。 |
| `taste_plan_bridge.json` | 将 `plans.json`、`plan.md` 摘要和 `experiment_plan.json` 汇总给后续模块。 |
| `blocker_action_plan.json` | 阻塞时由 blocker action 生成的行动建议。 |

`experiment_plan.json` 的关键策略：

- `status=selected_plan_ready` 只在存在唯一 `selected_plan_id` 时出现。
- 未选择、缺失选择或多选时分别保持 blocked 状态，后续模块必须停止。
- `selected_plan_contract` 只描述候选 artifact/data/protocol 需求和实验逻辑，不授权具体路径或命令。

## 4. 运行方式

所有命令建议在远端 TASTE 根目录执行，并先进入正确环境：

```bash
cd <TASTE_ROOT>
source /home/fmh/workspace/miniforge/etc/profile.d/conda.sh
conda activate ar_taste
export NVM_DIR=/home/fmh/workspace/.nvm
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
```

### 框架调用

```bash
python framework/scripts/run_module.py planning --action plan --run-id <run_id> --idea-id <idea_id> --repair-rounds 3
```

等价直接入口：

```bash
python modules/planning/main.py --action plan --run-id <run_id> --idea-id <idea_id> --repair-rounds 3
```

### 单独给定 idea 运行

```bash
python modules/planning/main.py --action plan \
  --idea-json modules/planning/examples/idea.json \
  --output-dir modules/planning/runs/demo_plan \
  --repair-rounds 3
```

无 LLM/Claude 烟测或本地调试可用：

```bash
python modules/planning/main.py --action plan \
  --backend off \
  --idea-text "你的创新方法 idea 文本" \
  --output-dir modules/planning/runs/manual_debug
```

Claude Code 后端示例：

```bash
python modules/planning/main.py --action plan \
  --backend claude_code \
  --idea-md modules/planning/examples/idea.md \
  --output-dir modules/planning/runs/claude_plan \
  --repair-rounds 3
```

`--backend off` 只用于调试 fallback 结构，不代表最终论文计划质量；正式使用建议配置 `--backend claude_code` 或 LLM。`--backend claude_code` 会调用 nvm 环境中的 Claude CLI，并把 prompt、schema、stdout/stderr 与解析结果写入当前输出目录的 `claude_runs/`。

### 选择最终执行 plan

生成候选后，如果需要将某个 plan 设为后续模块唯一执行合同，可通过已有 Python API 调用 `finish_plan(run_id, plan_id)`，或由项目 Claude Code/人类把 `plans.json` 中恰好一个 plan 标记为 `selected_for_execution=true` 并重新运行/同步 Planning 输出。

## 5. 运行流程逻辑

1. 归一化 idea：合并 `new_method/hypothesis`、`method_details/mechanism`、`initial_experiment/min_experiment` 等同义字段。
2. 生成初稿：要求输出方法设计、环境证据、repo/data 需求、步骤、基线/消融、指标、失败分析、go/no-go、Claude Code handoff、论文证据检查。
3. 多轮评估与修复：默认 3 轮。评估器专门检查 plan 是否可执行、是否越权绑定 Environment、是否缺少失败分析或论文证据门槛。
4. 写出候选计划：`plans.json` 保存所有版本；`plan.md` 给人类审阅。
5. 生成执行合同：`experiment_plan.json` 与 `taste_plan_bridge.json` 明确 selected/backlog 策略。
6. 项目同步：框架模式下同步到当前项目 state；standalone 模式默认只写 `modules/planning/runs/`，不写 planning 外中间产物。

## 6. 脚本结构

`modules/planning/scripts/` 根目录只保留兼容入口，真实实现按功能分类：

| 兼容入口 | 真实实现 | 功能 |
| --- | --- | --- |
| `scripts/plan_pipeline.py` | `scripts/core/plan_pipeline.py` | 核心计划生成、评估、修复、Markdown 渲染、执行合同生成。 |
| `scripts/planning_tools.py` | `scripts/tools/planning_tools.py` | Planning 私有工具集合：experiments、workflow、blocker_resolution、review_board、method_frontier、reflect。 |
| `scripts/build_blocker_action_plan.py` | `scripts/blockers/build_blocker_action_plan.py` | 根据项目阻塞状态生成结构化行动计划。 |
| `scripts/propose_next_actions.py` | `scripts/actions/propose_next_actions.py` | 根据当前状态排序下一步行动，供监督和修复循环使用。 |

根兼容入口必须保留，因为测试、框架和其它模块仍可能直接 import 或执行旧路径；新增业务逻辑应写到分类目录的真实实现中。

## 7. 维护规则

- 修改前先读本 README、`script_manifest.json` 和相关真实实现脚本；本机忽略的 `工作状态.txt` 若存在，只能作为维护交接背景。
- 只改 `modules/planning` 内文件；不改 web 前端、framework 或其它科研模块。
- standalone 调试输出放在 `modules/planning/runs/` 或 `modules/planning/.tmp/`，这些路径已被 `.gitignore` 排除。
- 不新增一次性脚本；小工具优先合并到现有分类实现。
- 计划文本要足够具体：方法作用点、实验矩阵、指标阈值、失败切片、反例压力、go/no-go 和论文证据门槛缺一不可。
- Planning 只提出候选执行要求；Environment 才能验证并确认具体 base/repo/data/runtime。
