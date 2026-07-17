# Planning 用户手册

Planning 把当前 Find 中已经人工批准的 Ideas 变成候选研究计划。正式项目中，Framework 是唯一调用入口；Web 只发送命令、显示任务状态并渲染 Framework 同步后的项目产物。

## 使用前准备

- 在 TASTE 根目录工作。
- 使用 conda 环境 `taste`。
- 正式运行前确保当前终端可以执行 `claude`。
- 当前 Find 必须已经完成 Read 和 Ideas。
- 至少明确批准一个 Idea。一次可以选择一个，也可以同时选择多个已批准 Ideas。

## 网页操作

1. 在“想法”页批准一个或多个 Ideas。
2. 打开“计划”页，在左上勾选本次要规划的 Ideas。默认勾选全部已批准项，也可以只保留一个。
3. 设置计划修复轮数，点击“生成计划”。
4. Claude Code 为每个已勾选 Idea 生成候选并直接写出最终 `plan.md`；程序只校验并生成机器投影，不重写正文。
5. 需要自动选择时，点击“让主控 Claude Code 选择唯一执行计划”；也可以在右上“计划操作”中由人选择一个执行计划。
6. 右上“计划操作”的“计划正文”直接修改最终 `plan.md`。保存时仍由 Framework 调用 Planning 校验并同步。
7. 页面下方右侧产物栏直接渲染项目中的 `plan.md`；它与右上编辑框不是第二份产物。

修复轮数是精确次数，默认 `3`；设为 `0` 时只生成 Claude 初版。历史 run 只能查看，不能生成、编辑、批准或选择当前项目的内容。

如果当前项目已经有 Plan，之后又修改了其中对应的 Idea，Framework 会先使旧的当前 Plan 失效，再按原来已规划且仍保持批准的 Idea 自动重生成。历史 Planning run 不会被删除。

进入 Environment、Experiment 或 Paper 前必须存在且只能存在一个 `selected_plan_id`。未选中的计划只作为候选，不会驱动下游执行。

## Framework 命令

以下命令都从 TASTE 根目录运行：

```bash
conda run -n taste python framework/scripts/main.py module planning \
  --action plan \
  --project <project> \
  --run-id <current_find_run_id> \
  --idea-id <approved_idea_id> \
  --repair-rounds 3
```

重复 `--idea-id` 可以选择多个已批准 Ideas；不传时默认使用全部已批准 Ideas。

让 Claude Code 从现有候选中选择唯一执行计划：

```bash
conda run -n taste python framework/scripts/main.py module planning \
  --action select \
  --project <project> \
  --run-id <current_find_run_id>
```

继续修复一个候选：

```bash
conda run -n taste python framework/scripts/main.py module planning \
  --action polish \
  --project <project> \
  --run-id <current_find_run_id> \
  --plan-id <plan_id> \
  --rounds 1
```

由人指定唯一执行计划：

```bash
conda run -n taste python framework/scripts/main.py module planning \
  --action finish \
  --project <project> \
  --run-id <current_find_run_id> \
  --plan-id <plan_id>
```

项目模式不要直接调用 `modules/planning/main.py`。Planning 会拒绝没有 Framework 授权和显式输入包的项目调用。

## 单独使用

Planning 可以脱离项目运行，输入 JSON 包含一个或多个 Ideas：

```json
{
  "ideas": [
    {
      "id": "idea-a",
      "title": "Candidate A",
      "new_method": "...",
      "method_details": "...",
      "initial_experiment": "..."
    }
  ]
}
```

运行：

```bash
conda run -n taste python modules/planning/main.py \
  --action plan \
  --idea-json /path/to/ideas.json \
  --repair-rounds 3
```

正式运行默认使用 Claude Code。`--backend off` 只用于离线结构冒烟测试，不代表正式计划质量。

## `plan.md` 格式

Claude Code 会读取当前 run 的 `ideas.json`，并直接写入同一 run 的 `plan.md`。每个候选按固定顺序包含：

1. `New Method`
2. 可选的 `Method Details`
3. `Initial Experiment`
4. `启发来源`
5. `Step-by-step Plan`
6. `Risks`
7. `Metrics`

写完后 Claude Code 会重新读取该文件，自查候选数量和 ID、栏目顺序、数学公式定界符、网页 Markdown 引用以及重复审计段。Planning 还会执行确定性发布校验；校验失败时不会把文件同步到项目。

## 产物说明

每次进程启动时先创建一个固定目录：

```text
modules/planning/.runtime/runs/<YYYYMMDDTHHMMSSffffffZ_action_pidPID>/
```

该进程的输入快照、中间结果、Claude Code 记录和最终结果只写入这个目录。完成或失败后会复制到：

```text
modules/planning/.runtime/latest_run/
```

`latest_run` 只供人检查，程序从不读取它。Framework 校验模块返回的精确 run 后，才把该 run 复制到：

```text
projects/<project>/planning/finding/planning_runs/<planning_run_id>/
```

当前项目使用的文件：

| 文件 | 用途 |
| --- | --- |
| `planning/finding/plan.md` | 唯一面向用户的计划正文，Web 直接渲染。 |
| `planning/finding/plans.json` | Plan/Idea ID、顺序、版本、选择状态和 Markdown 审计；不复制标题或正文。 |
| `state/experiment_plan.json` | 唯一选中计划的下游机器合同；未选择时保持 blocked。 |
| `state/taste_plan_bridge.json` | 轻量路径和选择索引；不嵌入正文或完整候选。 |

## 常见阻塞

- **没有已批准或已勾选的 Idea**：回到“想法”页批准至少一个，并在“计划”页勾选。
- **Read 或 Ideas 与当前 Find 不一致**：先重新运行对应当前 Find 的 Read/Ideas。
- **Claude Code 不可用**：检查用户自己的 Claude Code 安装和登录；正式项目不会用脚本伪造最终 `plan.md`。
- **`plan.md` 保存被拒绝**：检查固定栏目、候选 ID、公式 `$...$` / `$$...$$` 和网页链接 `[标题](<https://...>)`。
- **下游仍被阻断**：确认已由 Claude Code 或人选择唯一执行计划。
