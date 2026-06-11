# TASTE Python 包目录

本目录包含 TASTE 的 Python 包、FastAPI 后端、React/Vite 前端源码、测试和包级依赖声明。面向用户的安装、配置、启动、七个模块使用说明、参考致谢和许可证说明，请以仓库根目录 [README.md](../../README.md) 为准。

不要只复制 `modules/taste/` 运行 TASTE。TASTE 需要整个仓库中的 `scripts/`、`templates/`、`prompts/`、`.claude/`、前端源码和项目模板一起工作；具体科研项目、运行日志、下载仓库、数据集、论文草稿和本机密钥应留在本机运行态目录中，不提交到 Git。

## 目录作用

| 路径 | 作用 |
| --- | --- |
| `auto_research/` | Find/Read/Ideas/Plan/Web 后端等核心 Python 代码。 |
| `auto_research/web/client/` | React/Vite 前端源码。构建产物 `dist/` 不提交。 |
| `tests/` | 后端和流程回归测试。 |
| `requirements.txt` | TASTE 管理环境所需 Python 依赖。 |
| `LICENSE` | 包内许可证副本。根目录 README 中也有许可证说明。 |

## 开发入口

从仓库根目录运行：

```bash
PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts" python -m pytest modules/taste/tests -q
npm --prefix modules/taste/auto_research/web/client run build
scripts/start_web.sh
```

TASTE 默认只监听 `127.0.0.1:8765`。如部署在远端服务器，请通过 SSH tunnel 在本地浏览器打开网页。
