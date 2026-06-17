# Framework / TASTE 框架层

`framework/` 是 TASTE 的框架层，不属于七个科研模块中的任何一个。它负责项目创建、运行环境探测、任务队列、Claude Code 会话、跨模块编排、共享模型、公共资源和审计入口。具体科研能力必须放在 `modules/finding` 到 `modules/writing` 七个模块里；网页展示和 API 桥接必须放在 `web/`。

## 输入

- `config.example.json` 和 `runtime/.config.json`：本机 LLM、Node、Claude、Python 等运行配置。
- `framework/resources/templates/project.json`：新项目模板。
- `projects/<project>/project.json`：项目配置和研究画像。
- 七个模块产出的结构化产物：Find packet、Read packet、ideas、plans、environment gate、experiment registry、paper pipeline。

## 输出

- `projects/<project>/`：项目级状态、日志、报告、产物和运行记录。
- `runtime/`：TASTE 本机运行态、缓存、网页 pid/log、维护归档和大文件缓存。
- 网页 job 状态：由 `agent_state.py`、`server.py` 和 `project_bridge.py` 共同展示。

## 目录规划

| 路径 | 作用 |
| --- | --- |
| `scripts/auto_research/` | 跨模块共享 Python 包：配置、任务、Markdown、LLM、存储、路径和来源选择等基础能力；保留包名 `auto_research`，但物理位置归入框架 scripts。 |
| `scripts/` | 框架级入口脚本和共享后端包，只做项目创建、编排、运行环境、状态报告、Claude 会话和跨模块调用，不放阶段私有逻辑。 |
| `resources/templates/` | 项目和论文模板。旧根目录 `templates/` 已迁入这里。 |
| `resources/prompts/` | 框架级 prompt 模板和 subagent 启动提示。旧根目录 `prompts/` 已迁入这里。 |
| `resources/automation/` | subagent 协议、角色配置等自动化资源。旧根目录 `automation/` 已迁入这里。 |
| `resources/claude/` | TASTE 提供给项目 Claude Code 的 agent/command/skill 模板。它不是项目 Claude 的工作目录；项目 Claude 的科学状态必须在 `projects/<project>/`。 |
| `requirements.txt` / `pyproject.toml` | TASTE 管理环境依赖和包元信息。 |
| `script_manifest.json` | 框架脚本清单，供维护者判断归属和冗余。 |

## 运行逻辑

1. `create_project.py` 根据 `resources/templates/project.json` 创建 `projects/<project>/`。
2. `start_web.sh` 使用管理 Python 启动 FastAPI/前端服务，默认监听 `127.0.0.1:8765`。
3. `web/backend` 接收网页配置和命令，调用框架脚本或七个模块脚本。
4. `project_paths.py` 统一解析项目路径、资源路径、PythonPath 和运行配置。
5. `run_frontend.py` 是 Find 旧兼容入口；当前真正的 Find 逻辑在 `modules/finding`。
6. `claude_project_session.py` 管理项目级 Claude Code 会话。会话工作目录应是 `projects/<project>/`，不能把科学交接写到仓库根部。
7. `run_project.py`、`run_full_research_cycle.py`、`run_supervision_tick.py` 等只负责跨模块编排和 gate，不应塞入某一阶段的具体算法。

## 框架脚本分组

| 分组 | 脚本 | 作用 |
| --- | --- | --- |
| 项目与路径 | `project_paths.py`, `project_config.py`, `create_project.py`, `init_project.py`, `init_workspace.py`, `list_projects.py` | 项目创建、路径解析、配置 patch、初始化目录。 |
| 运行环境 | `runtime_env.py`, `detect_machine_profile.py`, `check_llm_ready.py`, `run_in_conda.sh`, `start_web.sh` | 检测 Python/Node/Claude/Conda/LLM 可用性并启动网页。 |
| 网页/Find 编排 | `run_frontend.py`, `sync_outputs.py`, `refresh_index_and_log.py`, `compile_prompt.py`, `export_obsidian.py`, `bootstrap_wiki.py`, `lint_wiki.py` | 网页任务桥、Find 产物同步、wiki/报告兼容输出。 |
| Claude 项目会话 | `claude_project_session.py`, `agent_state.py`, `record_safe_unblock_web_job.py`, `generate_handoff.py`, `work_status.py` | 项目代理队列、会话状态、handoff/status 记录。 |
| 全流程编排 | `run_project.py`, `run_autonomous_research.py`, `run_full_research_cycle.py`, `run_research_trajectory_supervisor.py`, `run_supervision_tick.py` | 从网页或自动科研入口串联七阶段；只负责编排和 gate。 |
| 历史/兼容 supervisor | `run_autoscientist_continuous.py`, `run_autoscientist_supervisor.py`, `run_evoscientist_style_cycle.py` | 早期自动科研循环兼容入口；后续应收敛到统一 full-cycle/supervision 入口。 |
| 审计与报告 | `research_healthcheck.py`, `report_status.py`, `audit_pipeline_runnability.py`, `audit_workflow_connectivity.py`, `audit_framework_content_coupling.py`, `audit_research_trajectory_capabilities.py`, `verify_research_trajectory_end_to_end.py`, `build_stagnation_report.py`, `research_manifest.py` | 检查当前项目、框架耦合、流程可运行性和长期轨迹能力。 |
| 共享工具 | `llm_client.py`, `pipeline_guard.py`, `taste_pythonpath.py`, `reconcile_state.py`, `refresh_project_reports.py`, `setup_git_guardrails.py`, `build_research_trajectory_system.py`, `update_evolution_memory.py` | LLM 调用、gate、防陈旧状态、PythonPath、git guard、trajectory memory。 |

## 冗余控制原则

- 新功能优先进入七个模块或 `web/`，不要把阶段逻辑继续塞进 `framework/scripts`。
- 框架脚本允许保留兼容 wrapper，但新主逻辑应按“大块能力”合并，不再新增一次性修补脚本。
- `runtime/` 是本机中间态、日志、缓存和维护归档的唯一根目录；不要恢复根 `logs/`、`reports/`、`state/`、`raw/` 等目录。
- `framework/resources/claude` 只是 TASTE 自带模板；项目 Claude Code 的工作记忆、handoff 和科学证据必须留在 `projects/<project>/`。
- 对 TASTE 逻辑修复要追根因，不能通过用户可见 fallback、硬编码研究主题或特定论文补丁来遮盖主流程问题。
