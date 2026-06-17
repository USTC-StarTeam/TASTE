# TASTE: 自动科研工作流

TASTE 是一个自动科研系统。它把论文发现、精读、想法生成、实验计划、环境配置、实验迭代和论文撰写放进同一个网页工作台：网页负责配置和状态展示，后端负责任务队列、运行记录和产物管理，Claude Code 项目代理负责真实代码、实验和论文修复。

默认网页地址：

```text
http://127.0.0.1:8765
```

## 必要环境

| 环境 | 用途 | 建议 |
| --- | --- | --- |
| Conda / Mamba + Python | 运行 TASTE 后端、调度脚本、Find 和审计脚本 | Python 3.10+，推荐 3.11 |
| Node.js + npm | 构建 React/Vite 网页前端 | 按 Node.js 官网推荐版本安装 |
| Claude Code CLI (`claude`) | Read/Ideas/Plan 的默认接管，以及 Environment/Experiment/Paper 项目代理 | 用户自己安装、登录和维护账号 |
| Find 阶段 LLM API | Find 的标题筛选、摘要评分和推荐排序 | 在网页里配置，密钥只保存到本机运行态配置 |

TASTE 区分两类 Python：

- `management_python`：运行 Web、调度、Find、审计等 TASTE 框架脚本；启动网页前就应该可用。
- `experiment_python`：运行具体科研项目的训练、评估和仓库脚本；在 Environment/环境配置阶段配置或由项目环境部署流程检测。

两者可以相同，但真实实验建议分离，避免训练依赖污染 TASTE 管理环境。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/USTC-StarTeam/TASTE.git TASTE
cd TASTE
```

### 2. 创建 Python 管理环境

Conda/Mamba：

```bash
conda create -n taste python=3.11 -y
conda activate taste
python -m pip install --upgrade pip
python -m pip install -r framework/requirements.txt
```

不用 Conda 时也可以用 venv：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r framework/requirements.txt
```

### 3. 安装 Node.js

Node.js、npm 和版本管理方式直接按 Node.js 官网下载页操作：<https://nodejs.org/zh-cn/download/>。

安装完成后确认：

```bash
node --version
npm --version
```

### 4. 安装并登录 Claude Code

TASTE 不配置 Claude Code 账号/API，也不会覆盖用户已有 Claude Code 设置。按 Claude Code 官方文档安装并登录即可：<https://docs.anthropic.com/en/docs/claude-code/setup>。

常用 npm 安装方式：

```bash
npm install -g @anthropic-ai/claude-code
```

确认当前终端可找到 Claude Code：

```bash
claude --version
```

### 5. 启动网页

```bash
cd TASTE
conda activate taste
framework/scripts/start_web.sh
```

启动脚本会自动补齐前端依赖、构建前端，并使用默认地址 `127.0.0.1:8765`。需要改端口时只设置端口变量：

```bash
WEB_PORT=<port> framework/scripts/start_web.sh
```

Windows 可以在 Git Bash、PowerShell 或 WSL 中运行。推荐安装 Git for Windows 后直接在 PowerShell 中调用同一个启动脚本：

```powershell
bash framework/scripts/start_web.sh
```

如果 TASTE 部署在服务器，远端仍然执行 `framework/scripts/start_web.sh`，本地浏览器通过 SSH tunnel 访问：

```bash
ssh -L 127.0.0.1:8765:127.0.0.1:8765 <user>@<server>
```

随后在浏览器打开：

```text
http://127.0.0.1:8765
```

## 配置教程

第一次打开网页后，先在左侧栏创建或选择项目，再认真填写研究画像、Find 阶段 LLM 和运行环境。研究画像建议写得尽量详细，它会直接影响论文匹配、标题筛选、摘要评分、idea 生成和后续 Claude Code 判断。投稿目标、实验 Python、实验轮数等配置分别在对应模块里设置，不放在全局侧栏。

配置保存原则：

| 类别 | 保存位置 | 说明 |
| --- | --- | --- |
| 公开模板 | `config.example.json`、`framework/resources/templates/project.json` | 给新用户和新项目提供默认结构，不含真实密钥和真实项目内容。 |
| 本机网页配置 | `runtime/.config.json` | 保存 LLM 密钥、邮件密码等本机运行态信息。 |
| 具体科研项目 | `projects/<project>/` | 保存项目配置、状态、运行记录、产物、仓库、数据和论文草稿。 |
| 前端静态产物 | 网页客户端构建目录 | 由启动脚本或 npm build 生成，网页服务读取这里的文件。 |

### 左侧栏配置

| 区域 | 配置项 | 作用 | 建议 |
| --- | --- | --- | --- |
| 项目 | 当前项目 | 选择正在操作的项目，网页状态、运行历史和产物都会跟随项目切换。 | 默认会加载最近使用的项目；需要新课题时再创建新项目。 |
| 项目 | 创建项目 / 项目 ID | 创建 `projects/<project>/`，建立稳定项目身份。 | 使用小写字母、数字、下划线或连字符，例如 `my_retrieval_study`。 |
| 项目 | 研究主题 | 当前项目的核心研究问题。 | 写成一句清楚的问题或方向；后续 Find、Read、Ideas、Plan 都会读取。 |
| 画像 | 研究兴趣 | 描述当前真正想匹配的论文范围。 | 尽量详细写研究问题、方法关键词、应用领域、正例/反例、偏好的 venue/年份、数据和代码要求；不要只写一个宽泛主题。 |
| 画像 | 研究者画像 | 描述已有项目、技术背景、实验条件、长期方向和评价标准。 | 写清楚可用算力、熟悉框架、数据访问限制、希望避免的方向和什么算“值得推进”；越具体，论文匹配和后续计划越稳定。 |
| LLM | provider | Find 阶段 LLM 供应商类型。 | OpenAI 兼容服务通常填 `openai_compatible`；也可按服务端支持填写。 |
| LLM | base URL | LLM API 地址。 | 填供应商提供的 `/v1` 兼容地址。 |
| LLM | model | Find 使用的模型名。 | 选择稳定、便宜、支持 JSON 输出的模型。 |
| LLM | API key | Find 阶段调用 LLM 的密钥。 | 只在网页保存；页面不会回显完整 key。 |
| LLM | temperature | Find 评分和少量兜底生成的随机性。 | 过滤/评分建议 0.2-0.6，默认 0.4。 |
| LLM | 角色 LLM 配置 | 高级兼容项：Find 可独立覆盖；Read/Ideas/Plan 只在 Claude Code 不可用时作为兜底。 | 普通使用留空，继承全局 LLM。 |
| LLM | 邮件配置 | 手动或自动发送当前 run 的 Markdown 产物。 | 不需要邮件通知时保持默认即可。 |
| 运行环境 | node_bin | Node/npm 所在目录。 | 自动检测优先；网页找不到 node/npm 时再手动填写。 |
| 运行环境 | claude_path | Claude Code 可执行文件路径。 | 普通终端能执行 `claude` 时通常无需填写；检测失败时填绝对路径。 |
| 运行环境 | management_python | TASTE 管理 Python。 | 启动网页的 Python 通常会自动写入；需要固定环境时手动填写。 |
| 运行环境 | extra_path | 额外 PATH 目录。 | 只在工具安装在非标准目录时填写，多个目录用 `:` 分隔。 |
| 历史运行 | run 列表 | 查看当前项目的历史 run、阶段和产物。 | 一个项目可以有多个历史运行；切换 run 只影响页面展示，不会修改产物。 |

### 七个模块配置

| 模块 | 配置项 | 作用 | 默认 / 建议 |
| --- | --- | --- | --- |
| Find | 会议搜索 | 从会议库里搜索会议/期刊。 | 默认只显示部分会议；用搜索框定位后点击添加。 |
| Find | 选择年份 | 设置“待添加年份”。 | 默认最新一年；修改年份不会改变已选会议，只有点击“添加”才写入该会议。 |
| Find | 已选会议 | 决定会议标题池来源。 | 默认全不选；同一会议可添加多个年份，并按年份独立抓取。 |
| Find | 检查可抓取 | 检查所选来源是否可访问并显示来源状态。 | 正式运行前建议点一次。 |
| Find | arXiv / bioRxiv / Nature / Science / HuggingFace / GitHub | 非会议来源开关。 | 默认全不选；只勾选本轮确实需要的来源。 |
| Find | arXiv 分类、检索词、日期 | 控制 arXiv 的分类、主题 query 和时间窗。 | 日期留空时默认最近 180 天；检索词留空时由主题自动生成。 |
| Find | bioRxiv 分类、日期 | 控制 bioRxiv 学科分类和时间窗。 | `all` 表示不过滤分类。 |
| Find | Nature / Science 预设、期刊、文章类型、日期 | 控制期刊流来源。 | 默认关闭；打开后再选择具体期刊和文章类型。 |
| Find | GitHub 语言 | 控制 GitHub 趋势榜语言过滤。 | 可填 `all`、`python`、`javascript` 等。 |
| Find | 推荐文章数量 | Find 最终展示的推荐论文上限。 | 默认 20；这是最终展示数量，不是抓取上限。 |
| Find | LLM 评估并发数 | 控制 Find 评分请求并发。 | 默认 8；慢速或限流 API 建议 4-8。 |
| Find | 高级预算 | 非会议抓取、arXiv query 数、会议标题扫描上限、召回池和详情池。 | 标准使用保持默认；`venue_title_scan_limit=0` 表示会议标题不设数量上限。 |
| Read | 运行精读 | 对当前 Find 推荐论文执行精读和边界审计。 | 需要先有当前 Find run。 |
| Read | 精读状态 | 展示推荐论文数、当前展示数、全文精读完成数和待补项。 | 主要用于判断 Read 是否拿到足够证据。 |
| Ideas | 想法最大数量 | 控制生成研究想法数量上限。 | 默认 6。 |
| Ideas | 想法生成并发数 | 控制并行生成 worker 数。 | 默认 2，范围 1-8。 |
| Ideas | 想法卡片编辑 | 编辑标题、新方法、初步实验和 Inspired by。 | 可以人工修正，再标记通过、待定或删除。 |
| Plan | 选择想法 | 从已通过想法中选择计划输入。 | 必须至少选择一个已通过想法。 |
| Plan | 修复轮数 | 生成计划时执行“草稿 -> 评估 -> 修复”的轮数。 | 默认至少 1。 |
| Plan | 候选计划操作对象 | 选择要继续润色或完成的候选计划。 | 不会默认把第一个候选当执行计划，需要显式选择。 |
| Plan | 让主控 Claude Code 选择唯一执行计划 | 将候选计划交给主控 Claude Code，形成执行合同。 | 进入实验前建议完成。 |
| Environment | conda 环境名称 | 具体科研项目使用的实验环境名。 | 在环境配置阶段填写；不是左侧栏全局配置。 |
| Environment | conda base | Conda/Mamba 安装根目录。 | 自动检测优先；检测失败时手动填写。 |
| Environment | 实验 Python | 训练和评估命令使用的 Python。 | 可由 conda base + 环境名派生，也可显式填写绝对路径。 |
| Environment | 真实创建 Conda 环境 | 是否让环境步骤实际创建/检查实验环境。 | 首次环境部署时开启；环境锁定后网页不再重复创建。 |
| Environment | 自然语言请求 | 给项目代理的环境部署说明。 | 说明仓库、数据、复现目标和限制。 |
| Experiment | 科研迭代轮数 | 控制实验子循环轮数。 | 从小轮数开始，确认日志和证据正常后再扩大。 |
| Experiment | 每轮最多实验数 | 控制每轮可启动的实验数量。 | 根据显存、时间和数据大小设置。 |
| Experiment | 执行实验计划 | 是否让项目代理按当前执行计划改代码并运行实验。 | 通常开启。 |
| Experiment | 准备环境计划 | 是否在实验阶段补做环境准备。 | 环境已锁定时一般无需重复准备。 |
| Experiment | 自动科研后跳过论文 | 实验完成后是否跳过论文阶段。 | 只想验证实验时开启。 |
| Paper | 投稿会议/期刊 | 论文模板、格式门控和页面限制目标。 | 只在论文撰写页配置；不要写在左侧栏主题里。 |
| Paper | 论文标题 | 论文草稿标题。 | 可先留空或写工作标题，后续由项目代理修订。 |
| Paper | 自动安装 LaTeX | 允许论文阶段尝试安装或补齐 LaTeX 工具。 | 服务器可安装依赖时开启；受限环境中关闭并手动准备。 |
| Paper | 生成与修订论文 | 启动论文代理，生成/修复 TeX、PDF、引用、图表和证据门控。 | 预览 PDF 不等于投稿通过；投稿状态以门控为准。 |

## 使用说明

### 左侧栏

左侧栏负责“项目级上下文”和“运行工具”。项目、研究兴趣、研究者画像会参与 Find、Read、Ideas、Plan 和后续项目代理判断；其中研究画像越详细，TASTE 越能区分“真正匹配的论文”和“只是泛泛相关的论文”。LLM 只负责 Find 的标题/摘要评分和少量兼容兜底；运行环境只负责让网页后端找到 `node`、`npm`、`claude` 和管理 Python。

TASTE 不管理 Claude Code 账号，也不会写入用户的 Claude Code API。只要当前用户终端能正常运行 `claude`，网页通常可以通过自动检测找到它；检测失败时再填写 `claude_path` 或 `extra_path`。

### Find：发现候选论文和代码线索

Find 的输入来自研究主题、研究兴趣、研究者画像、已选会议/年份和勾选的非会议来源。会议来源会先扫描所选会议/年份的标题池，再按主题相关性保留候选进入详情抓取；非会议来源按各自 API 或 RSS 获取候选。之后系统会抓取摘要/详情，用 Find LLM 做评分和排序，最终只展示通过真实摘要和评分门控的推荐论文。

Find 页面同时展示来源状态、调研验收计数和当前 run 产物。会议默认不设标题数量上限，`venue_title_scan_limit=0` 表示全扫；测试或异常源保护时才需要设正数。arXiv 只有勾选后才抓取，日期留空时默认最近 180 天。

### Read：精读推荐论文

Read 读取当前 Find run 的推荐论文，默认交给 Claude Code 根据论文、摘要、链接和可用全文信息做精读与边界审计。它关注论文真正解决了什么、证据是什么、可复现资源在哪里、哪些结论不能被当前信息支持。页面只展示当前 Find 对应的精读状态，避免把历史 run 的内容混入当前项目判断。

### Ideas：生成和筛选研究想法

Ideas 基于当前 Find/Read 产物生成可实验化的研究想法。每张卡片包含标题、新方法、初步实验和启发来源，用户可以直接编辑，也可以把想法标记为通过、待定或删除。只有通过的想法会进入 Plan 的候选输入。

默认情况下 Ideas 使用 Claude Code 接管；如果 Claude Code 不可用，才使用 LLM 兼容路线。想法数量和并发数只影响 Ideas，不影响 Read、Plan、Environment、Experiment 或 Paper。

### Plan：形成可执行实验计划

Plan 从已通过想法中选择输入，生成候选实验计划，并对计划执行评估和修复。候选计划不会自动变成主线执行计划；需要用户显式选择，或点击“让主控 Claude Code 选择唯一执行计划”生成执行合同。完成计划后，页面和 `plan.md` 只保留最终正文，评估与修复过程仍保留在结构化产物中。

### Environment：选择基底、检查数据并锁定实验环境

Environment 根据 Find/Plan 的证据选择最适合跟进的仓库或基底，检查真实数据/loader 是否可用，并准备具体科研项目使用的 Conda/Python 环境。这个阶段配置的是 `experiment_python`，它只服务训练、评估和外部仓库脚本，不应混同于 TASTE 管理 Python。

环境配置是一次性创建逻辑：成功创建或确认后会锁定，网页不会反复安装、修改或重建环境。之后 Experiment 和 Paper 复用已经锁定的环境状态与证据。

### Experiment：真实代码和实验迭代

Experiment 由 Claude Code 项目代理执行。它会围绕当前执行计划检查代码、修改实现、启动实验、读取日志/loss、分析坏例、记录指标和下一步行动。页面展示实验与复现门控、当前主线摘要、实验记录表和证据路径；旧历史记录不会被当成当前路线的新证据。

实验结果只有在真实数据、loader、复现和审计证据满足门控时才会进入可写论文的候选证据。失败、阻塞和负结果也会保留在记录中，用于后续判断。

### Paper：生成论文预览并执行门控修复

Paper 根据投稿会议/期刊、当前计划、实验记录和审计证据生成或修订论文。项目代理会检查官方模板、页面限制、引用渲染、图表质量、自审发现和证据门控。页面可以展示 PDF/TeX 预览，但预览稿不代表投稿通过；是否可投稿以门控状态为准。

如果实验或证据门控未通过，Paper 仍可生成目标 venue 预览，方便查看结构和格式，但不会把缺失证据包装成正式结论。

### 任务栏和产物

页面底部任务栏展示当前项目的 job/run 状态、阶段进度、最近日志、命令和产物路径。Find/Read/Ideas/Plan 的 Markdown 产物只在对应页面展开；Environment、Experiment、Paper 展示各自阶段的真实状态与证据，避免把文献调研日志误看成实验或论文结论。

## 重要目录

根目录只保留源码、配置样例、私有项目工作区和本机运行态。过去散落在根目录的 `prompts/`、`templates/`、`automation/`、`.claude/` 已迁入 `framework/resources/`；旧日志、维护报告、临时发现结果和缓存已归档到忽略的 `runtime/`。

```text
.
├── AGENTS.md                      # 维护 TASTE 仓库的 agent 规则，不是项目 Claude 研究交接
├── CLAUDE.md                      # 维护/运行 TASTE 时给 Claude Code 的仓库级说明
├── README.md                      # 用户说明与根目录规划
├── SECURITY.md
├── LICENSE
├── config.example.json            # 公开配置样例，不含密钥
├── framework/                     # TASTE 框架层、共享包、编排脚本、公共资源
│   ├── auto_research/
│   ├── scripts/
│   └── resources/
│       ├── templates/             # 项目/论文模板
│       ├── prompts/               # 框架 prompt 与 subagent 启动提示
│       ├── automation/            # subagent 协议和角色资源
│       └── claude/                # TASTE 提供的 Claude agent/command/skill 模板
├── modules/                       # 七个独立科研模块
│   ├── finding/
│   ├── reading/
│   ├── ideation/
│   ├── planning/
│   ├── environment/
│   ├── experimenting/
│   └── writing/
├── web/                           # FastAPI bridge 与 React/Vite 前端
├── tests/                         # 回归测试
├── projects/                      # 本机私有项目工作区，git 只保留 .gitkeep
├── runtime/                       # 本机运行态、日志、缓存、维护归档，整体忽略
└── third_party/                   # 外部参考 checkout，整体忽略
```

根目录已清理掉不应作为源码存在的历史目录：`handoff/`、`logs/`、`reports/`、`state/`、`status/`、`discover/`、`raw/`、`obsidian/`、`tmp/`、`.probe/`、`.runtime/`、`.agents/` 等都不再作为根目录规划存在；有保留价值的旧内容统一移动到 `runtime/maintenance/` 或 `runtime/logs/legacy_root/`。

常用入口：

| 路径 | 作用 |
| --- | --- |
| `framework/scripts/create_project.py` | 根据 `framework/resources/templates/project.json` 创建项目。 |
| `framework/scripts/start_web.sh` | 启动 Web/API，默认 `127.0.0.1:8765`。 |
| `framework/scripts/project_paths.py` | 统一项目路径、资源路径、PythonPath 和 runtime 配置。 |
| `framework/scripts/runtime_env.py` | 检测和构造 Python、Node、Claude Code 运行环境。 |
| `modules/finding/scripts/find_pipeline.py` | Find 主流水线。 |
| `modules/reading/main.py --action current_find_research_plan` | 当前 Find 对应的 Read/Ideas/Plan 主路由。 |
| `framework/scripts/claude_project_session.py` | 项目 Claude Code 会话和 guidance 队列；会话工作目录应是 `projects/<project>/`。 |
| `modules/environment/main.py --action run_environment_stage` | 环境配置阶段。 |
| `modules/experimenting/scripts/run_coding_agent.py` | 实验代理入口。 |
| `modules/writing/scripts/run_paper_orchestra_bridge.py` | 论文生成、修复和门控桥接。 |
| `web/backend/auto_research/web/server.py` | FastAPI 后端。 |
| `web/frontend/client/src/App.tsx` | React 前端主界面。 |

运行态项目目录：

```text
projects/<project>/
├── project.json
├── AGENTS.md / CLAUDE.md          # 项目 Claude Code 可读取的项目级规则
├── state/
├── planning/
├── reports/
├── logs/
├── runs/
├── artifacts/
├── repos/ 或 third_party/
└── paper/
```

这些目录通常包含私人路径、下载仓库、数据、日志、论文草稿和未公开结论，默认不提交。项目 Claude Code 的研究交接、工作记忆和科学状态只能留在项目目录，不能写回仓库根部。

## 模块边界

TASTE 的科研能力按七个阶段拆成 `modules/finding/`、`modules/reading/`、`modules/ideation/`、`modules/planning/`、`modules/environment/`、`modules/experimenting/`、`modules/writing/`。每个阶段目录都包含：

- `README.md`：中文说明模块输入、输出、运行逻辑、脚本清单和冗余控制原则。
- `main.py`：该阶段唯一公开后端入口，负责 action 路由，并通过 `--contract` 输出外部输入、输入产物、输出产物和职责边界。
- `script_manifest.json`：当前 `scripts/` 顶层脚本归属、函数、imports 和归属理由。

`modules/` 只放七个科研阶段模块。`framework/` 负责运行时、任务队列、项目状态、共享模型、公共资源和跨阶段编排；`web/` 负责 FastAPI bridge 与 React/Vite 前端，只做人类交互、配置修改、任务触发和产物展示。Find 不负责全文抓取；全文证据、阅读包和同轮可读 replacement 属于 Read-stage packet。Ideas/Plan 不应提前依赖 Environment 的确认结果；Environment/Experiment/Writing 只能消费显式产物和选择合同，不能隐式读取历史阶段状态来替代当前模块输入。

## 维护原则

- 优先让 TASTE 主流程一次跑对；fallback 只作为最后兼容路线，不能用来掩盖主流程质量问题，更不能把失败直接甩给用户网页。
- 修改前必须读清相关前后端和模块逻辑，找到根因后再改；禁止只按某篇论文、某个研究主题、某台机器路径写特例。
- 所有代码脚本不能包含当前研究项目、工作环境、API key 或本机绝对路径的硬编码。
- 中间态、日志、缓存和维护归档统一进 `runtime/`；项目运行产物进 `projects/<project>/`；源码根目录不再新增散落目录。
- 测试运行 TASTE 功能时，除 debug/单元测试外应通过网页操作，并亲自视觉检查对应页面的所有关键元素和产物文本。
- 每次较大修改要维护 `工作状态.txt`，并在工作树稳定后及时提交 git；该文件是本机维护状态，不提交。

## 参考与致谢

本项目在设计和实现过程中参考了若干项目的思路与部分实现方式。相关第三方项目各自遵循其原始许可证，TASTE 在本仓库中保留可审计的来源说明。

- **iDeer**：研究助手流程、信息源聚合、报告生成和邮件报告设计。
- **openccf**：CCF 目录结构、会议/期刊元数据组织和 DBLP 抓取策略；本仓库内置的 `modules/finding/data/ccf_venues.json` 是基于 openccf 公开 CCF 数据整理出的归一化 venue catalog。
- **[ICLR2026-Guide-CN](https://github.com/JenniferZhao0531/ICLR2026-Guide-CN)**：OpenReview/ICLR 论文收集、组织和展示方式。
- **[ccf-deadlines](https://github.com/ccfddl/ccf-deadlines)**：会议信息组织、截止日期元数据和用户侧 venue 工作流设计。
- **[academic-research-skills](https://github.com/Imbad0202/academic-research-skills)**：Claude Code 学术研究技能组织方式，以及 research -> write -> review -> revise -> finalize 的阶段化写作/审稿思路。
- **[PaperOrchestra](https://github.com/Ar9av/PaperOrchestra)**：多代理论文写作、outline/plot/literature/section/refinement 分工、引用核验、图表检查和论文质量自评思路。
- **[nature-skills](https://github.com/Yuan1z0825/nature-skills)**：面向 Nature 风格论文的学术表达、图表规范和写作检查技能设计。

TASTE 也感谢 FastAPI、React、Vite、Claude Code 以及 arXiv、bioRxiv、Nature、Science、HuggingFace、GitHub、DBLP/CCF/会议索引等公开工具与数据源；这些基础设施让本地可审计的科研自动化成为可能。

## 许可证

TASTE 使用 GNU Affero General Public License v3.0。详见 [LICENSE](LICENSE)。
