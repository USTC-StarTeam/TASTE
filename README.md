# TASTE: 自动科研工作流与本地研究代理

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-local%20API-009688)
![React](https://img.shields.io/badge/React-Vite-61DAFB)
![Claude Code](https://img.shields.io/badge/Claude%20Code-project%20agent-6f42c1)
![License](https://img.shields.io/badge/License-AGPL--3.0-blue)

**TASTE** 是一个本地优先的自动科研系统。它把论文发现、精读、idea、plan、环境配置、实验迭代和论文撰写放进同一个可审计工作流：网页负责配置、任务队列、状态展示和人类 guidance；后端负责抓取、评分、产物管理和门控；Claude Code 项目代理负责真实项目代码、实验和论文修复。

本仓库只包含框架代码、模板、网页和测试。真实研究项目目录 `projects/*`、运行配置 `runtime/.config.json`、日志、下载仓库、数据集、实验产物、生成论文和 API key 都是本机运行态内容，默认被 Git 忽略，不应提交到公开仓库。

默认网页地址：

```text
http://127.0.0.1:8765
```

## 目录

- [系统定位](#系统定位)
- [前置依赖](#前置依赖)
- [跨平台建议](#跨平台建议)
- [安装 TASTE](#安装-taste)
- [Claude Code 安装与使用](#claude-code-安装与使用)
- [启动网页](#启动网页)
- [SSH 远端网页显示](#ssh-远端网页显示)
- [网页配置说明](#网页配置说明)
- [完整使用流程](#完整使用流程)
- [产物与目录](#产物与目录)
- [开发与验证](#开发与验证)
- [发布前安全检查](#发布前安全检查)
- [常见问题](#常见问题)
- [许可证](#许可证)

## 系统定位

TASTE 的公开代码与具体科研项目解耦。仓库本身不绑定某个论文题目、模型、数据集、API 供应商或本机目录。每个具体研究项目都应该在 `projects/<project>/` 下独立运行，拥有自己的主题、配置、运行历史、产物和状态。

主要模块：

| 模块 | 作用 | 默认执行路线 |
| --- | --- | --- |
| Find | 会议、期刊、arXiv、bioRxiv、Hugging Face、GitHub 等来源发现与评分 | LLM 用于标题/摘要评分和少量判断 |
| Read | 对 Find 推荐论文做精读和证据整理 | Claude Code 优先，必要时可回退到 LLM |
| Ideas | 生成、评审和筛选研究想法 | Claude Code 优先，必要时可回退到 LLM |
| Plan | 生成和修订研究计划 | Claude Code 优先，必要时可回退到 LLM |
| Environment | 配置实验环境、数据、loader、参考复现证据 | Claude Code 项目代理和审计脚本 |
| Experiment | 迭代实验、记录指标、分析失败、导入证据 | Claude Code 项目代理和实验脚本 |
| Paper | 生成论文、修复引用/图表/格式、执行投稿门控 | Claude Code 项目代理和论文审计脚本 |

运行原则：

- Find 可以独立使用 LLM；后三个重模块 Environment、Experiment、Paper 不走通用 LLM 路线，而是由用户本机已经配置好的 Claude Code CLI 接管项目。
- Read、Ideas、Plan 默认让 Claude Code 接管当前 Find 结果；如果没有可用 Claude Code，才使用保留的 LLM 回退路线。
- 网页上简单状态可以是确定性状态映射；项目代理回复区域应显示 Claude Code 的真实输出；黑底日志保留详细运行日志，但不把所有内部审计噪声都放进人类摘要。
- 会议来源默认广泛抓取所选会议/年份的标题池；`venue_title_scan_limit=0` 表示不设数量上限。测试时才设置正数。
- arXiv 只有在用户勾选时才抓取；日期留空时默认抓近 180 天。

## 前置依赖

### 必需依赖

| 依赖 | 用途 | 建议 |
| --- | --- | --- |
| Git | 克隆仓库、版本管理 | 2.30+ |
| SSH client | 访问远端服务器、端口转发 | OpenSSH、Git Bash、PowerShell SSH 均可 |
| Conda/Mamba | 管理 Python 环境 | Miniforge、Mambaforge、Anaconda 均可 |
| Python | Web、后端脚本、测试 | Python 3.10+，推荐 3.11 |
| Node.js + npm | 构建 React/Vite 前端 | Node 20+，推荐 Node 22 |
| ripgrep (`rg`) | 快速搜索和部分检查 | 推荐安装 |
| Claude Code CLI (`claude`) | 后续项目代理、实验、论文修复 | 用户自行安装和登录 |

### 可选依赖

| 依赖 | 用途 |
| --- | --- |
| CUDA/GPU 驱动 | 运行深度学习实验，按具体项目决定 |
| TeX Live / MacTeX / MiKTeX | 论文 PDF 编译 |
| GitHub CLI (`gh`) | 可选的 GitHub 操作辅助 |
| build-essential / Xcode Command Line Tools | 编译部分 Python 或论文项目依赖 |

### 管理环境与实验环境

TASTE 明确区分两套 Python：

| 环境 | 作用 | 配置位置 |
| --- | --- | --- |
| 管理 Python | 启动 Web、运行调度、审计、Find/Read/Plan 等框架脚本 | `MANAGEMENT_PYTHON` 或网页“运行环境” |
| 实验 Python | 运行具体科研项目的训练、评估、仓库脚本 | `EXPERIMENT_PYTHON` 或网页“运行环境/环境配置” |

这两者可以相同，但推荐分离。管理环境要稳定，实验环境可以随具体论文仓库安装 CUDA、PyTorch、旧版依赖或数据处理包。

## 跨平台建议

### Linux

Linux 是最直接的运行环境。推荐使用 Miniforge 或 Mambaforge 创建管理环境，用 `nvm` 安装 Node 22，再安装 Claude Code。

常见系统依赖示例：

```bash
sudo apt update
sudo apt install -y git curl openssh-client build-essential ripgrep
```

如果系统没有 `rg`，也可以在 Conda 环境中安装：

```bash
conda install -c conda-forge ripgrep -y
```

### macOS

macOS 可直接运行 TASTE。推荐安装 Xcode Command Line Tools、Miniforge、nvm 和 Claude Code。

```bash
xcode-select --install
```

如果使用 Homebrew，也可以安装基础工具：

```bash
brew install git ripgrep nvm
```

### Windows

推荐使用 **WSL2 + Ubuntu** 运行 TASTE 服务端。Windows 原生浏览器通过 `http://127.0.0.1:8765` 或 SSH tunnel 访问网页。这样能避免 Python、shell、CUDA、LaTeX 和 Claude Code 工具链在原生 Windows 上的差异。

推荐路径：

1. 安装 WSL2 和 Ubuntu。
2. 在 Ubuntu 里安装 Conda、nvm、Node、Claude Code、TASTE。
3. 在 Windows 浏览器里打开 TASTE 网页。
4. 如果 TASTE 跑在另一台远端 Linux 服务器，则在 Windows PowerShell 或 Windows Terminal 里使用 `ssh -L` 做端口转发。

原生 Windows 也可以安装 Claude Code、Node 和 Git for Windows，但 TASTE 的实验仓库、bash 脚本和科研依赖通常更适合 WSL2。

## 安装 TASTE

### 1. 克隆仓库

```bash
git clone https://github.com/USTC-StarTeam/TASTE.git TASTE
cd TASTE
```

如果你使用 fork 或私有仓库，把 URL 换成自己的仓库地址即可。

### 2. 创建管理 Python 环境

Conda/Mamba 示例：

```bash
conda create -n taste python=3.11 -y
conda activate taste
python -m pip install --upgrade pip
python -m pip install -r modules/taste/requirements.txt
```

venv 示例：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r modules/taste/requirements.txt
```

如果要在同一台机器上跑真实实验，建议额外创建实验环境：

```bash
conda create -n taste_exp python=3.11 -y
```

具体实验依赖由后续 Environment 阶段根据选中的代码仓库、数据和任务安装。

### 3. 安装 Node.js 和前端依赖

推荐用 nvm 安装 Node 22：

```bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install 22
nvm use 22
node --version
npm --version
```

安装并构建前端：

```bash
npm --prefix modules/taste/auto_research/web/client install
npm --prefix modules/taste/auto_research/web/client run build
```

说明：

- `tsc` 和 `vite` 来自 `modules/taste/auto_research/web/client/node_modules/.bin/`。
- `client/node_modules/` 是 npm 在 TASTE 前端目录下安装的本地依赖目录，不是需要提交到 Git 的源码。
- `scripts/start_web.sh` 如果发现 `client/dist/` 不存在，会自动尝试 `npm install && npm run build`。公开部署仍建议先手动构建一次，方便提前暴露 Node/npm 问题。

### 4. 创建公开模板之外的本机配置

```bash
mkdir -p runtime
cp config.example.json runtime/.config.json
```

`config.example.json` 是唯一应该提交的公开配置模板。`runtime/.config.json` 是本机运行配置，默认被 `.gitignore` 忽略。不要把 API key、SMTP 密码、私有 base URL 或本机路径写进公开文件。

也可以不手动复制模板，第一次打开网页后在配置面板保存；后端会写入 `runtime/.config.json`。

### 5. 创建或选择项目

新项目示例：

```bash
python scripts/create_project.py   --name my_project   --topic "your research topic"   --prompt "your concrete research goal"   --query "initial search query"
```

这会创建：

```text
projects/my_project/
```

每个项目的主题、Find 选择、运行历史、论文产物和实验状态都在自己的项目目录中。不同项目之间不要共享 `projects/<project>/`。

## Claude Code 安装与使用

TASTE 不安装、不登录、不改写 Claude Code 账号，也不写入 Claude Code API key。用户必须先在自己的系统里安装并登录 `claude` CLI，TASTE 只检测并调用这个命令。

官方文档入口：

- Claude Code Quickstart: <https://docs.anthropic.com/en/docs/claude-code/quickstart>
- Claude Code Setup: <https://docs.anthropic.com/en/docs/claude-code/setup>
- Claude Code Authentication: <https://docs.anthropic.com/en/docs/claude-code/iam>

### 推荐安装方式

macOS、Linux、WSL：

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Windows PowerShell：

```powershell
irm https://claude.ai/install.ps1 | iex
```

Windows CMD：

```cmd
curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd
```

Homebrew：

```bash
brew install --cask claude-code
```

WinGet：

```powershell
winget install Anthropic.ClaudeCode
```

npm 方式也可用，但需要 Node.js 18+：

```bash
npm install -g @anthropic-ai/claude-code
```

不要使用 `sudo npm install -g` 安装 Claude Code，容易造成权限和升级问题。

### 登录和验证

```bash
claude --version
claude
```

第一次运行 `claude` 会提示登录。按照浏览器提示完成登录；如果你在 WSL、SSH 或容器里登录，浏览器可能无法回调到终端，此时按官方提示复制登录 URL 或登录 code，再粘回终端。登录后可以在 Claude Code 会话里输入：

```text
/login
/help
```

Claude Code 需要有 Claude Pro、Max、Team、Enterprise、Console 或受支持云供应商访问权限。免费 Claude.ai 计划通常不包含 Claude Code 权限。

### TASTE 如何使用 Claude Code

- 网页“运行环境”里可以点击自动检测，或手动填写 `claude_path`。
- 如果 `claude` 已在 PATH 中，通常不需要手动填写。
- TASTE 会把项目目录、当前阶段、guidance 和审计要求传给 Claude Code 项目代理。
- Claude Code 的账号、权限、模型、供应商和个人设置仍由用户自己的 Claude Code 安装管理。
- `.claude/settings.json`、`.claude/projects/`、Claude 本地会话和凭证都不应提交到 Git。

## 启动网页

### 单机启动

在仓库根目录执行：

```bash
conda activate taste
export WORKSPACE_ROOT="$PWD"
export PROJECT_ID=my_project
export DEFAULT_PROJECT_ID=my_project
export PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts"
export MANAGEMENT_PYTHON="$(command -v python)"

# 如果使用 nvm，把 Node bin 放进 PATH；也可以在网页运行环境里保存 node_bin。
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 22
export NODE_BIN="$(dirname "$(command -v node)")"

WEB_HOST=127.0.0.1 WEB_PORT=8765 scripts/start_web.sh
```

然后打开：

```text
http://127.0.0.1:8765
```

健康检查：

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/api/frontend/version
```

### 重要环境变量

| 变量 | 作用 |
| --- | --- |
| `WORKSPACE_ROOT` | TASTE 仓库根目录。通常设为 `$PWD`。 |
| `PROJECT_ID` | 当前默认项目 ID。 |
| `DEFAULT_PROJECT_ID` | 没有显式项目时使用的项目 ID。 |
| `PYTHONPATH` | 至少包含 `$PWD/modules/taste:$PWD:$PWD/scripts`。 |
| `MANAGEMENT_PYTHON` | Web/调度/审计使用的 Python。 |
| `EXPERIMENT_PYTHON` | 实验运行使用的 Python。可在网页里配置。 |
| `NODE_BIN` | Node/npm 所在目录。用于前端构建和运行时检测。 |
| `WEB_HOST` | 默认 `127.0.0.1`。公开部署不建议改成 `0.0.0.0`。 |
| `WEB_PORT` | 默认 `8765`。 |

LLM 的 provider/base/model/key 推荐在网页配置面板或 `runtime/.config.json` 保存。环境变量只作为空配置时的启动兜底：

```bash
export LLM_PROVIDER=openai_compatible
export LLM_API_BASE=<chat-completions-compatible-base-url>
export LLM_MODEL=<model-name>
export OPENAI_API_KEY=<api-key>
```

## SSH 远端网页显示

推荐远端服务器只监听 `127.0.0.1`，本地浏览器通过 SSH tunnel 访问。

### 1. 在远端启动 TASTE

```bash
ssh <user>@<server>
cd /path/to/TASTE
conda activate taste
export WORKSPACE_ROOT="$PWD"
export PROJECT_ID=my_project
export DEFAULT_PROJECT_ID=my_project
export PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts"
export MANAGEMENT_PYTHON="$(command -v python)"
WEB_HOST=127.0.0.1 WEB_PORT=8765 scripts/start_web.sh
```

保持这个终端运行，或用 `tmux` / `screen` / systemd 管理。

### 2. 在本机建立端口转发

macOS、Linux、WSL、Git Bash、Windows PowerShell 都可以使用：

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <user>@<server>
```

打开本机浏览器：

```text
http://127.0.0.1:8765
```

如果本机 8765 已被占用，换成本机端口 18765：

```bash
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 <user>@<server>
```

打开：

```text
http://127.0.0.1:18765
```

如果远端 Web 不是 8765，例如远端是 9000，本机仍想用 8765：

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:9000 <user>@<server>
```

### 3. 常用排查

远端检查服务是否在监听：

```bash
curl http://127.0.0.1:8765/health
```

本机检查 tunnel 后是否通：

```bash
curl http://127.0.0.1:8765/health
```

如果浏览器打不开，优先检查：

- 远端 TASTE 服务是否还在运行。
- SSH tunnel 命令是否还在运行。
- 本机端口是否被其他程序占用。
- 远端是否错误绑定到了公网地址或其他端口。

## 网页配置说明

网页配置是普通用户的权威编辑入口。不要为了改配置去手写多个脚本或多个 JSON。公开仓库里只有 `config.example.json` 模板；运行时全局配置落在 `runtime/.config.json`；项目独立配置落在 `projects/<project>/project.json`。

### 项目与研究主题

左侧项目区域用于创建、切换和保存项目。研究主题、研究画像、初始检索 query 应属于具体项目，不属于框架代码。

注意：投稿目标只应在 Paper/论文撰写部分配置和展示，不应作为全局侧栏主题的一部分。

### LLM 配置

LLM 配置主要服务 Find：

| 字段 | 说明 |
| --- | --- |
| Provider | 供应商标签，例如 `openai_compatible`。 |
| Base URL | Chat Completions 兼容接口地址。 |
| Model | Find 标题/摘要评分使用的模型。 |
| API Key | 只保存在运行态配置，不返回给浏览器明文，不提交 Git。 |
| Temperature | Find 评分采样温度。推荐低温。 |
| 并发数 | Find 评估并发，默认 8；慢速或限流 API 可设 4 到 8。 |

角色 LLM 配置只用于覆盖早期轻量阶段或 Claude Code 不可用时的回退路线。Environment、Experiment、Paper 不应依赖这里的通用 LLM。

### 邮件配置

邮件配置是可选功能，用于手动或自动发送阶段报告。默认 `manual_enabled=true`、`auto_send_enabled=false`。只有在明确需要邮件通知时才填写 SMTP server、sender、receivers 和 password。SMTP 密码是本机运行态秘密，不提交 Git。

### 运行环境

网页“运行环境”用于检测和保存本机工具路径：

| 字段 | 说明 |
| --- | --- |
| `management_python` | 管理 Python。启动 Web 和框架脚本。 |
| `experiment_python` | 实验 Python。运行训练、评估和具体项目脚本。 |
| `conda_base` | Conda/Mamba 根目录，用于诊断和推导环境。 |
| `nvm_dir` | nvm 根目录，例如 `$HOME/.nvm`。 |
| `node_bin` | Node/npm 可执行目录。 |
| `claude_path` | Claude Code 可执行文件路径。为空时从 PATH 检测。 |
| `extra_path` | 额外 PATH 条目。 |

初始化服务时 TASTE 会自动检测常见位置并预填；检测失败时，用户应在网页里手动填写真实路径，再重新测试运行环境。

### Find 配置

标准使用通常只需要配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 推荐文章数量 | 20 | Find 最终推荐给后续 Read 的上限。 |
| LLM 评估并发 | 8 | Find 标题/摘要评分并发。慢速 API 建议 4 到 8。 |
| 会议/年份选择 | 最新年份优先 | 搜索栏里的年份只是待添加年份；只有点击会议卡片的添加按钮才会修改已选会议。 |
| 非会议来源 | 默认不强制开启 | arXiv、bioRxiv、Hugging Face、GitHub、Nature、Science 只有勾选才进入本次 Find。 |

高级预算默认折叠。它们主要用于测试、限流和异常数据源保护：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 非会议初始抓取上限 `max_fetch_papers` | 120 | arXiv/bioRxiv 等非会议来源初抓数量。会议来源不使用它。 |
| 会议标题全扫保护上限 `venue_title_scan_limit` | 0 | 0 表示所选会议/年份不设数量上限；正数只用于测试或保护。 |
| 标题扫描比例 `venue_title_scan_fraction` | 1.0 | 1 表示全扫已抓到的标题池。 |
| 主题候选保留上限 `find_recall_count` | 1000 | 标题池按主题召回后进入后续详情抓取前的上限。不是最终推荐数。 |
| 详情评分预算 `detail_fetch_count` | 160 | 进入摘要/详情抓取和评分的候选预算。 |
| 摘要评分最大并发 | 8 | 最终摘要评分阶段并发。遇到限流可降低。 |
| arXiv 最大检索词数 | 3 | 每轮最多请求几个 arXiv query。 |
| arXiv 每个检索词数量 | 50 | 每个 query 请求数量。 |
| arXiv 日期 | 空 | 起止都为空时默认最近 180 天。 |

### Read、Ideas、Plan 配置

这些模块的运行数量在各自页面设置，不放在全局侧栏里：

- Read 页面选择要精读的论文和数量。`max_papers=0` 表示读取当前 Find 推荐里的全部可读候选。
- Ideas 页面设置生成 idea 数量、候选倍率和并行 worker。
- Plan 页面选择 idea 并设置修复轮数。

这些阶段的产物跟随 run ID 和项目走，历史运行不会覆盖其他项目。

### Environment、Experiment、Paper 配置

- Environment 页面负责基底仓库、数据、loader、Conda/Python、参考复现证据和环境审计。
- Experiment 页面负责实验迭代、指标、失败分析、项目代理 guidance 和完整回复。
- Paper 页面负责投稿目标、模板、PDF 预览、引用/图表/格式/证据门控。

论文投稿目标只在 Paper 页面配置。不要把具体会议目标写进全局主题栏或框架模板。

## 完整使用流程

### 1. 创建项目并打开网页

```bash
python scripts/create_project.py --name my_project --topic "..." --prompt "..." --query "..."
PROJECT_ID=my_project DEFAULT_PROJECT_ID=my_project scripts/start_web.sh
```

### 2. 保存运行环境

在网页里：

1. 打开“运行环境”。
2. 点击自动检测。
3. 检查管理 Python、实验 Python、Node、Claude Code、Conda 是否通过。
4. 不通过就手动填写路径并保存。

### 3. 配置 Find 所需 LLM

在侧栏填写 Find LLM 的 provider/base/model/key。保存后执行“检查可抓取性”或直接运行 Find。

### 4. 选择来源并运行 Find

1. 在会议搜索区选择会议。
2. 在“选择年份”里保留或添加年份。
3. 点击会议卡片的添加按钮，把会议/年份加入已选会议。
4. 需要 arXiv 或 GitHub 等非会议来源时再勾选。
5. 启动 Find。

同一个会议可以选择多个年份，并作为同一个会议下的多个年份展示和运行。

### 5. 运行 Read、Ideas、Plan

Find 完成后进入 Read 页面，选择当前 Find run 的推荐论文并运行精读。之后依次进入 Ideas 和 Plan 页面。默认会尽量使用 Claude Code 接管当前 Find 的后续研究计划；如果 Claude Code 不可用，系统才使用 LLM 回退路线。

### 6. 运行 Environment 和 Experiment

Environment 需要真实基底仓库、数据/loader 证据和可执行环境。TASTE 会记录环境审计、参考复现和阻塞原因。通过后进入 Experiment，由项目代理执行实验、导入指标和修复失败。

### 7. 运行 Paper

Paper 页面设置投稿目标，生成或修订论文预览。系统会检查引用渲染、图表、claim ledger、实验支撑、页数、模板和投稿门控。证据不足时只允许预览，不标记投稿通过。

### 8. 查看任务历史和日志

- 任务历史按项目和 run ID 组织。
- 黑底日志区域展示详细运行日志，便于调试。
- 人类摘要区域只展示可读状态和关键阻塞，不应显示内部 hash mismatch、原始栈迹或给代理看的修复清单。

## 产物与目录

仓库核心结构：

```text
.
├── README.md
├── START_HERE.md
├── config.example.json
├── templates/project.json
├── modules/taste/
│   ├── auto_research/web/server.py
│   ├── auto_research/web/project_bridge.py
│   └── auto_research/web/client/
├── scripts/
├── .claude/
├── prompts/
├── automation/
└── projects/.gitkeep
```

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

常用文件和脚本：

| 路径 | 作用 |
| --- | --- |
| `scripts/create_project.py` | 从模板创建项目。 |
| `scripts/start_web.sh` | 启动本地 Web/API 服务；必要时自动构建前端。 |
| `scripts/runtime_env.py` | 统一检测和构造 PATH、PYTHONPATH、Node、Claude、Python 环境。 |
| `scripts/ensure_current_find_research_plan.py` | 让 Claude Code 接管当前 Find 的 Read/Ideas/Plan。 |
| `scripts/claude_project_session.py` | Claude Code 项目会话封装和 guidance 队列消费。 |
| `scripts/run_environment_stage.py` | 环境配置阶段入口。 |
| `scripts/run_coding_agent.py` | 实验项目代理入口。 |
| `scripts/run_paper_orchestra_bridge.py` | 论文生成、修复与投稿门控桥接。 |
| `modules/taste/auto_research/web/server.py` | FastAPI 后端、job 队列、配置 API、WebSocket。 |
| `modules/taste/auto_research/web/client/src/App.tsx` | React 单页网页。 |
| `modules/taste/tests/` | 回归测试。 |

## 开发与验证

后端测试：

```bash
PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts" python -m pytest modules/taste/tests -q
```

前端构建：

```bash
npm --prefix modules/taste/auto_research/web/client run build
```

脚本语法检查：

```bash
python -m py_compile   scripts/runtime_env.py   modules/taste/auto_research/web/project_bridge.py   modules/taste/auto_research/web/server.py

bash -n scripts/start_web.sh
```

启动后 smoke check：

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/api/frontend/version
curl http://127.0.0.1:8765/api/config
```

Git 检查：

```bash
git status --short
git diff --check
```

## 发布前安全检查

提交前应该只包含框架代码、模板、测试和文档。不要提交任何本机运行态或私有科研内容。

不要提交：

- `runtime/.config.json`
- `.claude/settings.json`
- `.claude/projects/`
- `projects/*` 真实项目目录，除 `projects/.gitkeep`
- `logs/`、`runtime/`、`tmp/`、`.runtime/`
- `third_party/`、`modules/writing/vendor/`
- 下载论文 PDF、数据集、模型 checkpoint、外部仓库
- API key、SMTP password、Claude Code 凭证、供应商 token
- 未公开实验结果、论文草稿、审稿回复、私有研究结论

推荐扫描：

```bash
git ls-files | rg '(^|/)(config\.json|\.claude/settings\.json|projects/|logs/|runtime/|tmp/|third_party/|.*\.log$|.*\.pid$|.*\.pdf$)'
```

除 `projects/.gitkeep` 和公开模板外，不应命中真实运行文件。

检查硬编码本机路径：

```bash
rg -n '/home/[^ ]+/workspace|/Users/[^ ]+|C:\Users|sk-[A-Za-z0-9]|api[_-]?key|smtp_password' README.md START_HERE.md config.example.json templates scripts modules/taste prompts automation .claude
```

注意：日志里显示绝对路径是正常的；关键是框架代码、脚本和公开文档不能写死某台机器的根路径。

## 常见问题

### 只复制几个文件能跑完整 TASTE 吗？

不能。TASTE 需要整个已跟踪仓库，包括 `scripts/`、`modules/taste/`、`.claude/`、`prompts/`、`templates/`、前端代码和测试。少数文件通常只是某次修改的变更集，不是完整可运行系统。

### `tsc` 或 `vite` 找不到怎么办？

在前端目录安装 npm 依赖：

```bash
npm --prefix modules/taste/auto_research/web/client install
npm --prefix modules/taste/auto_research/web/client run build
```

`tsc` 和 `vite` 是前端 npm 依赖，不需要全局安装，也不应把 `node_modules/` 提交到 Git。

### `claude` 找不到怎么办？

先在普通终端确认：

```bash
claude --version
```

如果终端能找到但网页检测不到，在网页“运行环境”里填写 `claude_path`，或把 Claude Code 所在目录加入 `extra_path` / shell PATH。不要把 Claude Code 账号或 API key 写进 TASTE 仓库。

### 为什么要用 SSH tunnel？

TASTE 网页会展示项目路径、运行日志、模型配置状态和未公开科研内容。默认监听 `127.0.0.1`，远端访问应通过 SSH tunnel，而不是把 Web 服务直接暴露到公网。

### Find 为什么看起来抓很多标题？

会议来源默认尽量全扫所选会议/年份标题池，再由主题召回、详情抓取和 LLM 评分决定精读候选。最终推荐数量由“推荐文章数量”控制。测试时可以设置 `venue_title_scan_limit` 为正数加速，但标准使用应保持 0。

### arXiv 默认抓多久？

如果勾选 arXiv 且开始/结束日期都留空，默认抓最近 180 天。需要指定年份或时间窗时，在 Find 的日期配置里填写 `YYYY-MM-DD` 或 `YYYY/MM/DD`。

### 网页配置保存在哪里？

全局网页配置在 `runtime/.config.json`，项目配置在 `projects/<project>/project.json`。二者都是运行态文件，不提交 Git。公开仓库只保留 `config.example.json` 和 `templates/project.json`。

### 可以把已有项目目录一起上传 GitHub 吗？

不建议。`projects/*` 通常包含私有路径、API 状态、下载仓库、数据、日志、论文草稿和未公开结论。公开发布只提交框架代码、模板、文档和测试。

### 论文 PDF 编译失败怎么办？

先确认系统安装了 LaTeX 发行版。Linux 常用 TeX Live，macOS 常用 MacTeX，Windows/WSL 可用 TeX Live 或 MiKTeX。论文页面会显示可读阻塞；详细编译日志保存在项目目录的 paper 运行产物中。

## 许可证

TASTE 使用 GNU Affero General Public License v3.0。详见 [modules/taste/LICENSE](modules/taste/LICENSE)。
