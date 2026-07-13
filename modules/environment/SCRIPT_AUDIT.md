# Environment Script Audit

本文件回答两个问题：Environment 为什么还需要这些 Python 文件，以及哪些工作已经交给 Claude Code、skill 或 prompt。

## 当前边界

- 唯一公开入口：`modules/environment/main.py`。
- Python 源文件：10 个，其中 `main.py` 1 个、`scripts/` 私有后端 9 个。
- 顶层函数：342 个；类：0 个。完整函数名见 `script_manifest.json`。
- 无必要的 `__init__.py` 已删除。
- Framework/Web 的 Python 路径不暴露 `modules/environment/scripts`。
- Environment 不导入、不调用 Framework；Framework 只能执行 `main.py`。
- 每次调用必须运行在 Conda `taste` 中。

## 三类处理

| 分类 | 处理结果 |
| --- | --- |
| 1. 入口、Claude 会话管理、输入输出和硬 gate | 保留最少 Python。代码负责唯一入口、固定 run、项目到唯一会话的映射、排队/抢占/恢复、命令执行、路径边界、真实回执和稳定 JSON 契约。 |
| 2. Claude 完成环境部署所需的复用知识 | 收敛到 `skills/environment-deployment/SKILL.md`。包括仓库核验、run-local Conda、依赖修补、数据/loader、机器适配、复现证据和审计标准。 |
| 3. 仅靠精确任务描述即可完成的工作 | 收敛到 `prompts/*.md`。仓库候选审阅、仓库发现、环境计划和审计裁决均由同一 Environment 主控 Claude 的独立 turn 完成。 |

代码只保留不能由自然语言可靠替代的确定性职责。语义判断由 Claude 完成，代码验证其输出结构、执行事实和证据门槛。

## 为什么每个脚本仍存在

| 文件 | 必须保留的职责 | 不能只用 prompt 的原因 |
| --- | --- | --- |
| `main.py` | 唯一公开 action 路由；强制 `taste`；创建固定 run；调用私有部署器；提供 `chat`、项目级 `status`；处理取消信号；保留项目引用的 run 并清理陈旧 run。 | 它定义模块公开边界、并发目录归属、取消边界和稳定退出码。 |
| `scripts/common/claude_runner.py` | 维护 `project -> session_id`；首次用固定 `--session-id`，后续只 `--resume`；强制 Claude cwd 为项目目录；串行化部署、审计和 Web 指令；支持排队、SIGTERM 抢占、取消、恢复和完整会话历史。 | 唯一会话、文件锁、进程抢占和并发顺序必须由代码保证。 |
| `scripts/common/io_utils.py` | JSON/text 原子读写、时间、slug/hash 和路径包含检查。 | 产物完整性和路径边界必须确定。 |
| `scripts/common/plan_schema.py` | 将不同来源的选定计划归一为固定 Environment 输入；只提取结构化仓库、数据和指标字段。 | Framework 与独立调用需要同一稳定输入契约。 |
| `scripts/common/shell.py` | 命令 token 化、危险命令拒绝、环境变量清理、run-local 缓存隔离、超时和日志回执。 | Claude 负责提出命令，代码必须控制实际执行。 |
| `scripts/environment/runtime_probe.py` | 探测本机 Conda、GPU、CUDA、CPU 和工具路径。 | 机器事实必须来自真实探测，不能由模型猜测。 |
| `scripts/repository/repo_manager.py` | GitHub clone、origin/commit 校验、损坏 clone 修复及 README/配置/入口证据收集。 | 仓库身份、commit 和本地文件存在性必须可验证。 |
| `scripts/reproduction/paper_evidence.py` | 从计划、本地论文、URL 和全文包收集受限文本与目标指标。 | Claude 需要可追溯输入，网络/文件读取和大小边界由代码执行。 |
| `scripts/reproduction/decision.py` | 指标数值解析、比较、成功标准 schema 和 verdict 固定字段规范化。 | 数值比较、允许的裁决值和布尔字段属于硬 gate。 |
| `scripts/orchestration/autonomous_deploy.py` | 串联计划归一、机器/论文/仓库证据、Claude turns、受控命令、receipts、workspace audit、approval/handoff gate 和最终裁决。 | 外部命令、真实证据、退出码和跨轮次状态必须由一个确定性事务编排。 |

## `autonomous_deploy.py` 为什么仍较大

该文件有 188 个顶层函数，但它们属于同一部署事务，没有第二套 Environment 实现。主要硬 gate 如下：

| 功能组 | 代码职责 | Claude 职责 |
| --- | --- | --- |
| 入口与 run 守卫 | 只接受 `main.py` 预创建的 run。 | 无。 |
| 仓库边界 | 校验 GitHub URL、clone 路径、origin、commit。 | 依据论文和文档选择或发现仓库。 |
| Prompt 渲染 | 绑定 skill、输入证据和唯一输出路径。 | 写候选审阅、发现、部署计划和审计 JSON。 |
| Conda/路径/命令边界 | 将环境重写到 run-local prefix；校验 cwd、参数、环境变量和危险命令。 | 根据真实日志设计安装与修复命令。 |
| 执行与 receipts | 逐命令执行、超时、git clone 重试、日志和回执。 | 根据失败回执提出下一轮修复。 |
| 指标 gate | 绑定允许阶段的成功回执，解析并比较数值。 | 解释论文目标、配置匹配和证据含义。 |
| Handoff gate | 强制 repo、Conda、数据、loader/model smoke、必需命令和 workspace audit 全部有证据。 | 输出固定审计检查、失败分类和修复计划。 |
| 最终契约 | 写 `environment_deployment_decision.json` 和退出码。 | 只能建议 `approve`、`reject` 或 `continue_repair`。 |

这些函数共享同一 run、同一 receipts 和同一最终 gate。拆成多个可执行脚本会重新产生绕过 `main.py` 的入口，因此维持为一个私有编排文件。

## Claude、Skill 与 Prompt

Environment 为每个项目维护一个主控 Claude 会话：

```text
modules/environment/.runtime/controllers/<project>/controller.json
```

部署计划、仓库审阅、仓库发现、审计裁决和 Web 指令都是该会话中的不同 turn。审计 turn 必须重新读取本轮证据，但不会新建第二个会话。

Skill：

| 文件 | 用途 |
| --- | --- |
| `skills/environment-deployment/SKILL.md` | Environment 主控可复用的部署、修复、证据和裁决规范。 |

Prompt：

| 文件 | 唯一任务 |
| --- | --- |
| `prompts/repo_candidate_review.md` | 只审阅输入计划列出的仓库候选。 |
| `prompts/repo_discovery.md` | 只在候选不足时依据论文/计划证据发现一个可信 GitHub 仓库。 |
| `prompts/environment_plan.md` | 只输出可执行的 run-local Conda、数据、smoke 和复现 JSON 计划。 |
| `prompts/audit_judgement.md` | 只依据本轮证据输出固定审计项、失败分类、修复计划和三值裁决。 |

## 会话与并发

- Framework 传入项目名；Environment 自己创建并保存唯一会话 ID。
- Claude 的 cwd 固定为 `projects/<project>/`。
- `execution.lock` 保证同项目只能有一个活跃 Environment Claude turn。
- 普通 Web 消息进入模块队列。
- 对话没有排队等待超时；消息只会完成、被明确取消或被更高优先级指令抢占后恢复。
- 优先 Web 消息记录入队后终止当前 Claude 进程组。
- 优先消息完成后，同一会话恢复原模块 prompt；再次被抢占时重复该流程，直到必需 JSON 已写出。
- 缺失必需 JSON 时硬 gate 返回失败，不把 Claude 的零退出码当作完成。

## Run 与同步

- `main.py` 在进程开始时创建 `.runtime/runs/<UTC微秒_action_pid>/`。
- 私有部署器只能使用本进程的预创建 run。
- `.runtime/latest_run` 是指向最近完成 run 的人工审查符号链接。
- 每个项目和 action 保留最近 5 个完成 run；项目 handoff/latest state 引用的 run 永远不会被自动清理。
- Environment 和 Framework 都不复制 run。
- Framework 读取明确的 `run_dir`，只向 `projects/<project>/state/` 写轻量投影。
- Web 部署：Web -> Framework 单阶段编排器 -> `modules/environment/main.py`。
- Web 对话：Web -> Framework `run_module.py` -> `modules/environment/main.py --action chat` -> 模块队列。

## Conda 名称口径

1. Web 已保存 `conda_env`：Framework 原样传给模块，所有 Claude 轮次固定使用。
2. Web 未填写：第一轮 Environment 主控 Claude 选择名称，模块写入 `conda_environment.json` 并固定。
3. Framework 将名称写入项目配置；handoff 通过后再写入真实 `conda_env_prefix` 和 `experiment_python`。
4. 后续实验使用项目配置中由 handoff 固定的 prefix/Python，不从名称重新猜测环境路径。

## 已删除的实现类型

- 所有无必要 `__init__.py`。
- 独立 status 脚本和可绕过 `main.py` 的旧 Environment action 脚本。
- Python 自动仓库替换、自由文本 URL 猜测、项目特例依赖修补和语义失败分类。
- Python 自动生成 success criteria、机器适配结论、数据可信结论和论文配置结论。
- run 复用、run 复制和项目内 Environment run 副本。
- Framework 内部导入 Environment 私有编排器的第二套执行路径。
