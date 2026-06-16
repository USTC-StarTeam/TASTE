# TASTE Python 包目录

本目录只包含 TASTE 的跨模块 Python 框架包和包级依赖声明。FastAPI 后端在 `../web/backend/`，React/Vite 前端在 `../web/frontend/client/`，七个科研阶段分别在 `../modules/` 下。面向用户的安装、配置、启动、七个模块使用说明、参考致谢和许可证说明，请以仓库根目录 [README.md](../README.md) 为准。

不要只复制 `framework/` 运行 TASTE。TASTE 需要整个仓库中的 `modules/`、`web/`、`framework/scripts/`、`modules/*/scripts/`、`templates/`、`prompts/`、`.claude/` 和项目模板一起工作；具体科研项目、运行日志、下载仓库、数据集、论文草稿和本机密钥应留在本机运行态目录中，不提交到 Git。

## 目录作用

| 路径 | 作用 |
| --- | --- |
| `auto_research/` | 跨阶段共享的框架代码，例如配置、存储、Markdown、任务边界和通用模型。 |
| `../web/backend/` | FastAPI 后端、网页任务桥和项目状态 API。 |
| `../web/frontend/client/` | React/Vite 前端源码。构建产物 `dist/` 不提交。 |
| `../modules/` | finding、reading、ideation、planning、environment、experimenting、writing 七个可独立运行的科研阶段。 |
| `modules/finding/data/quality/` | 包内静态会议/期刊质量表，只提供确定性元数据和小幅排序信号。 |
| `tests/` | 后端和流程回归测试。 |
| `requirements.txt` | TASTE 管理环境所需 Python 依赖。 |
| `LICENSE` | 包内许可证副本。根目录 README 中也有许可证说明。 |

## 开发入口

从仓库根目录运行：

```bash
PYTHONPATH="$PWD/framework:$PWD/web/backend:$PWD/framework/scripts:$PWD/modules/finding:$PWD/modules/reading:$PWD/modules/ideation:$PWD/modules/planning:$PWD/modules/environment:$PWD/modules/experimenting:$PWD/modules/writing:$PWD/modules/finding/scripts:$PWD/modules/reading/scripts:$PWD/modules/ideation/scripts:$PWD/modules/planning/scripts:$PWD/modules/environment/scripts:$PWD/modules/experimenting/scripts:$PWD/modules/writing/scripts:$PWD" python -m pytest tests -q
npm --prefix web/frontend/client run build
framework/scripts/start_web.sh
```

TASTE 默认只监听 `127.0.0.1:8765`。如部署在远端服务器，请通过 SSH tunnel 在本地浏览器打开网页。
