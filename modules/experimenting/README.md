# Experimenting / 实验迭代模块

Experimenting 使用每个项目唯一的主控 Claude Code 会话完成实验设计、代码修改、真实运行、失败修补、指标整理和实验记录。不同项目使用不同会话；Environment、Experimenting、Writing 也各自使用独立会话。

## 使用要求

- 管理命令必须在 Conda `taste` 环境运行。
- 必须指定 `projects/<project>/` 下已经存在的项目。
- 项目必须先有当前选中的 Idea/Plan 和 Environment handoff。
- Claude Code 必须已由用户自己的 Claude 配置授权；模块不会写入账号或 API 配置。

Framework 是 Web 和模块之间的唯一调用者。命令行单独使用模块时，也只能调用 `main.py`，不能直接执行 `scripts/`。

## 继续实验工作

```bash
cd <TASTE_ROOT>
conda run -n taste python modules/experimenting/main.py \
  --action work \
  --project <project> \
  --iterations 1
```

`work` 会把“继续完成 Experimenting 本职工作”的任务发送给该项目已有的 Experimenting 主控会话。不存在会话时模块创建一个，以后该项目的实验按钮和网页对话都只恢复这个会话。

Claude 的工作目录固定为：

```text
projects/<project>/
```

它从当前项目读取 selected Idea/Plan、Environment handoff、Find/Read 证据和已有实验记录，并在需要时更新执行级实验方案。

## 与主控对话

```bash
conda run -n taste python modules/experimenting/main.py \
  --action chat \
  --project <project> \
  --message "检查当前失败实验，先修复最关键 blocker，再继续原实验任务。"
```

主控忙碌时，消息进入该项目的 Experimenting 模块队列。网页会显示排队消息，主控完成当前操作后按顺序处理。

需要让网页指令立即优先时：

```bash
conda run -n taste python modules/experimenting/main.py \
  --action chat \
  --project <project> \
  --message "立即停止当前方向，先检查这条人工修正。" \
  --interrupt-current
```

主控会中止当前 Claude 调用、优先完成新指令，然后在同一会话中恢复被中断的 Experimenting 工作。

## 实验记录顺序

每个新实验必须按以下顺序完成：

1. 读取当前研究合同和 Environment handoff。
2. 更新执行级实验方案。
3. 修改选定仓库并运行真实实验。
4. 等待实验和最终验证命令结束。
5. 从最终输出解析指标、失败、坏例和反例。
6. 写入 artifact record、项目 registry、CSV 和 Markdown 实验表。
7. 运行 Experimenting 审计并修补未通过项。

完成态 registry 行必须包含 `validation_finished_at`、`validation_return_code=0` 和不早于验证结束时间的 `recorded_at`。模块的确定性 Gate 会阻止记录顺序不成立的结果，并命令同一主控会话修补一次；仍未通过时本次工作返回阻塞。

## 运行与审计工具

主控需要确定性工具时仍通过 `main.py` 调用：

| Action | 用途 |
| --- | --- |
| `runtime_env` | 记录指定 Conda 运行环境。 |
| `launch` | 用项目实验 Python 启动一个带独立 artifact 目录、PID 和日志的实验进程。 |
| `watchdog` | 检查项目实验进程、重复 writer、解释器和产物污染。 |
| `audit_iteration` | 新开 Claude 审计会话检查实验轮次证据。 |
| `runtime_integrity` | 新开 Claude 审计会话检查运行完整性。 |
| `reference_reproduction` | 新开 Claude 审计会话检查参考复现证据。 |
| `audit_adjudication` | 对指定审计包进行独立裁决。 |
| `controller_status` | 查看项目对应的 Experimenting 会话状态。 |

具体参数可运行：

```bash
conda run -n taste python modules/experimenting/main.py --action <action> --help
```

## 运行产物

`work`、`chat`、`controller_status` 不创建 run，也不为每条消息创建新工作区。它们固定使用：

```text
modules/experimenting/.runtime/controllers/<project>/
```

会话映射、队列、提示、日志和最近回执保存在：

```text
modules/experimenting/.runtime/controller_sessions.json
modules/experimenting/.runtime/controllers/<project>/
```

`runtime_env`、`launch`、`watchdog` 和独立审计等确定性工具需要不可变进程回执，因此工具进程使用：

```text
modules/experimenting/.runtime/runs/<精确UTC时间_action_pid>/
```

同一工具进程始终使用这个目录。结束后模块复制一份到 `.runtime/latest_run/`，它只供人工审查，程序不读取它；主控工作和网页对话不使用 `latest_run`。

项目可见的主控状态和最近回复保存在：

```text
projects/<project>/state/experimenting_controller.json
projects/<project>/state/experimenting_controller_last_result.json
projects/<project>/reports/experimenting_controller.md
```

## Web 与 Framework

- Web 只向 Framework 提交 `experiment` 或 `experimenting-chat` 请求。
- Framework 只把项目、消息、迭代数和排队/中断选项传给 Experimenting 公共入口。
- 实验按钮和网页对话使用同一个项目唯一 Experimenting 会话。
- Web 不调用模块脚本、不维护会话映射、不复制模块产物。
- full-cycle 只按顺序触发 Find、Read、Idea、Plan、Environment、Experimenting、Writing，效果等同于依次点击七个阶段按钮。

剩余私有脚本及其保留理由见 `SCRIPT_AUDIT.md`。
