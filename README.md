# TASTE: 本地自动科研工作流

TASTE 是一个本地优先的自动科研系统。它把论文发现、精读、idea、plan、环境配置、实验迭代和论文撰写放进同一个网页工作流：网页负责配置和状态展示，后端负责任务队列和产物管理，Claude Code 项目代理负责真实代码、实验和论文修复。

本仓库只提交框架代码、模板、网页和测试。真实项目目录 `projects/*`、运行配置 `runtime/.config.json`、日志、下载仓库、数据集、实验结果、论文草稿和 API key 都是本机运行态内容，默认不进入 Git。

默认网页地址：

```text
http://127.0.0.1:8765
```

## 必要环境

只需要准备这些环境即可运行 TASTE：

| 环境 | 用途 | 建议 |
| --- | --- | --- |
| Conda / Mamba + Python | 运行 TASTE 后端、调度脚本和测试 | Python 3.10+，推荐 3.11 |
| Node.js + npm | 构建 React/Vite 网页前端 | 按 Node.js 官网推荐版本安装；需要版本管理时按官网页面选择 nvm 等方式 |
| Claude Code CLI (`claude`) | Read/Idea/Plan 的 Claude 接管，以及 Environment/Experiment/Paper 项目代理 | 用户自己安装、登录和维护账号 |
| Find 阶段 LLM API | Find 的标题筛选、摘要评分和推荐排序 | 在网页配置，密钥不要提交 Git |

TASTE 区分两类 Python：

- `management_python`：运行 Web、调度、Find、审计等框架脚本。
- `experiment_python`：运行具体科研项目的训练、评估和仓库脚本。

两者可以相同，但真实实验建议分离。

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
python -m pip install -r modules/taste/requirements.txt
```

如果你不用 Conda，也可以用 venv：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r modules/taste/requirements.txt
```

### 3. 安装 Node.js 并构建前端

Node.js、npm 和 nvm/版本管理方式直接按 Node.js 官网下载页操作：<https://nodejs.org/zh-cn/download/>。

安装完成后确认：

```bash
node --version
npm --version
```

构建网页：

```bash
npm --prefix modules/taste/auto_research/web/client install
npm --prefix modules/taste/auto_research/web/client run build
```

`tsc` 和 `vite` 来自 `modules/taste/auto_research/web/client/node_modules/.bin/`。`node_modules/` 是本机 npm 依赖目录，不提交 Git。

### 4. 安装并登录 Claude Code

TASTE 不配置 Claude Code 账号/API，也不会覆盖用户已有 Claude Code 设置。请先在自己的机器或远端服务器上安装并登录 `claude`。

官方文档：

- <https://docs.anthropic.com/en/docs/claude-code/quickstart>
- <https://docs.anthropic.com/en/docs/claude-code/setup>
- <https://docs.anthropic.com/en/docs/claude-code/iam>

常用安装方式：

macOS / Linux / WSL：

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Windows PowerShell：

```powershell
irm https://claude.ai/install.ps1 | iex
```

npm 方式：

```bash
npm install -g @anthropic-ai/claude-code
```

验证：

```bash
claude --version
claude
```

第一次运行会提示登录。WSL、SSH 或容器里如果浏览器回调失败，按 Claude Code 提示复制登录 URL 或 code，再粘回终端即可。

### 5. 创建项目

```bash
python scripts/create_project.py \
  --name my_project \
  --topic "your research topic" \
  --prompt "your concrete research goal" \
  --query "initial search query"
```

项目会创建在：

```text
projects/my_project/
```

每个项目的主题、配置、运行历史和产物都独立保存。

### 6. 启动网页

```bash
export WORKSPACE_ROOT="$PWD"
export PROJECT_ID=my_project
export DEFAULT_PROJECT_ID=my_project
export PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts"
export MANAGEMENT_PYTHON="$(command -v python)"

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 22
export NODE_BIN="$(dirname "$(command -v node)")"

WEB_HOST=127.0.0.1 WEB_PORT=8765 scripts/start_web.sh
```

打开：

```text
http://127.0.0.1:8765
```

健康检查：

```bash
curl http://127.0.0.1:8765/health
```

`scripts/start_web.sh` 会自动设置 `WORKSPACE_ROOT`、`PYTHONPATH` 等默认值；上面显式写出来是为了部署时更容易排查环境。

Windows PowerShell 原生启动也可以不用 bash 脚本，前提是已经完成前端构建：

```powershell
$env:WORKSPACE_ROOT = (Get-Location).Path
$env:PROJECT_ID = "my_project"
$env:DEFAULT_PROJECT_ID = "my_project"
$env:PYTHONPATH = "$($PWD.Path)\modules\taste;$($PWD.Path);$($PWD.Path)\scripts"
$env:MANAGEMENT_PYTHON = (Get-Command python).Source
$env:NODE_BIN = Split-Path (Get-Command node).Source
python -m uvicorn auto_research.web.server:app --host 127.0.0.1 --port 8765
```

如果 Windows 装了 Git Bash，也可以在 Git Bash 里直接使用上面的 `scripts/start_web.sh` 启动方式。

## 远端服务器访问网页

推荐远端只监听 `127.0.0.1`，本地浏览器通过 SSH tunnel 访问。

远端启动 TASTE：

```bash
ssh <user>@<server>
cd /path/to/TASTE
conda activate taste
PROJECT_ID=my_project DEFAULT_PROJECT_ID=my_project WEB_HOST=127.0.0.1 WEB_PORT=8765 scripts/start_web.sh
```

本机建立端口转发：

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <user>@<server>
```

本机浏览器打开：

```text
http://127.0.0.1:8765
```

如果本机 8765 被占用：

```bash
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 <user>@<server>
```

然后打开：

```text
http://127.0.0.1:18765
```

Windows PowerShell、macOS Terminal、Linux shell 和 WSL 里都可以使用同样的 `ssh -L` 命令。

## 网页里需要配置什么

第一次打开网页后，按这个顺序配置即可：

1. 项目：选择或创建当前项目，确认研究主题和研究画像。
2. 运行环境：点击自动检测；检查 `management_python`、`experiment_python`、`node_bin`、`claude_path`。不通过就手动填路径。
3. LLM：填写 Find 阶段使用的 provider、base URL、model 和 API key。API key 只保存在运行态配置，不提交 Git。
4. Find 来源：选择会议/年份，必要时勾选 arXiv、GitHub、Hugging Face 等非会议来源。
5. Paper：投稿目标只在论文撰写页面配置，不放在全局主题栏。

配置保存位置：

| 文件 | 作用 | 是否提交 Git |
| --- | --- | --- |
| `config.example.json` | 公开配置模板 | 是 |
| `runtime/.config.json` | 本机网页配置和密钥 | 否 |
| `templates/project.json` | 新项目模板 | 是 |
| `projects/<project>/project.json` | 具体项目配置 | 否 |

## 工作流

```text
Find -> Read -> Ideas -> Plan -> Environment -> Experiment -> Paper
```

- Find 使用 LLM 做标题/摘要筛选和推荐排序。
- Read、Ideas、Plan 默认让 Claude Code 接管当前 Find 结果；Claude Code 不可用时才回退到 LLM 路线。
- Environment、Experiment、Paper 使用 Claude Code 项目代理和审计脚本，不走通用 LLM 路线。
- 网页任务历史、日志和产物都跟随项目和 run ID 保存。

Find 默认逻辑：

- 会议来源默认扫描所选会议/年份的标题池。
- `venue_title_scan_limit=0` 表示不设标题数量上限；只有测试或异常数据源保护时才设正数。
- arXiv 只有勾选时才抓取；日期留空时默认最近 180 天。
- 最终推荐数量由“推荐文章数量”控制，默认 20。

## 重要目录

```text
.
├── README.md
├── START_HERE.md
├── config.example.json
├── templates/project.json
├── scripts/
├── modules/taste/
│   └── auto_research/web/
├── prompts/
├── automation/
├── .claude/
└── projects/.gitkeep
```

常用入口：

| 路径 | 作用 |
| --- | --- |
| `scripts/create_project.py` | 创建项目。 |
| `scripts/start_web.sh` | 启动 Web/API。 |
| `scripts/runtime_env.py` | 检测和构造 Python、Node、Claude Code 运行环境。 |
| `scripts/ensure_current_find_research_plan.py` | 让 Claude Code 接管当前 Find 的 Read/Ideas/Plan。 |
| `scripts/claude_project_session.py` | Claude Code 项目会话和 guidance 队列。 |
| `scripts/run_environment_stage.py` | 环境配置阶段。 |
| `scripts/run_coding_agent.py` | 实验代理入口。 |
| `scripts/run_paper_orchestra_bridge.py` | 论文生成、修复和门控。 |
| `modules/taste/auto_research/web/server.py` | FastAPI 后端。 |
| `modules/taste/auto_research/web/client/src/App.tsx` | React 网页。 |

运行态项目目录：

```text
projects/<project>/
├── project.json
├── state/
├── reports/
├── runs/
├── artifacts/
├── repos/ 或 third_party/
└── paper/
```

这些目录通常包含私人路径、下载仓库、数据、日志、论文草稿和未公开结论，不提交 Git。

## 验证

后端测试：

```bash
PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts" python -m pytest modules/taste/tests -q
```

前端构建：

```bash
npm --prefix modules/taste/auto_research/web/client run build
```

启动检查：

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/api/frontend/version
```

提交前检查：

```bash
git status --short
git diff --check
```

## 不要提交的内容

不要提交：

- `runtime/.config.json`
- `.claude/settings.json`
- `.claude/projects/`
- `projects/*` 真实项目目录，除 `projects/.gitkeep`
- `logs/`、`runtime/`、`tmp/`、`.runtime/`
- 下载论文、数据集、模型 checkpoint、外部仓库
- API key、SMTP password、Claude Code 凭证、供应商 token
- 未公开实验结果、论文草稿、审稿回复、私有研究结论

推荐扫描：

```bash
git ls-files | rg '(^|/)(config\.json|\.claude/settings\.json|projects/|logs/|runtime/|tmp/|third_party/|.*\.log$|.*\.pid$|.*\.pdf$)'
```

除公开模板和 `projects/.gitkeep` 外，不应命中真实运行文件。

## 常见问题

### 只复制几个文件能跑 TASTE 吗？

不能。TASTE 需要整个已跟踪仓库，包括 `scripts/`、`modules/taste/`、`.claude/`、`prompts/`、`templates/` 和前端代码。

### `tsc` 或 `vite` 找不到怎么办？

运行：

```bash
npm --prefix modules/taste/auto_research/web/client install
npm --prefix modules/taste/auto_research/web/client run build
```

它们是前端本地 npm 依赖，不需要全局安装。

### `claude` 找不到怎么办？

先确认：

```bash
claude --version
```

如果普通终端能找到但网页检测不到，在网页“运行环境”里填写 `claude_path`，或把 Claude Code 所在目录加入 `extra_path`。

### arXiv 默认抓多久？

勾选 arXiv 且开始/结束日期都留空时，默认抓最近 180 天。需要指定年份或时间窗时，在 Find 页面填写日期。

## 许可证

TASTE 使用 GNU Affero General Public License v3.0。详见 [modules/taste/LICENSE](modules/taste/LICENSE)。
