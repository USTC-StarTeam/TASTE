# TASTE Python Package Directory

This directory contains the TASTE Python package, FastAPI backend, React/Vite frontend source, tests, and package-level dependency metadata. For user-facing installation, configuration, startup, module usage, acknowledgements, and license notes, use the repository root [README.md](../../README.md) as the single source of truth.

Do not copy only `modules/taste/` to run TASTE. The framework needs the full tracked repository, including `scripts/`, `templates/`, `prompts/`, `.claude/`, frontend source, and project templates. Concrete research projects, run logs, downloaded repositories, datasets, paper drafts, and local credentials belong to local runtime directories and should not be committed.

## Directory Map

| Path | Purpose |
| --- | --- |
| `auto_research/` | Core Python code for Find/Read/Ideas/Plan/Web backend. |
| `auto_research/web/client/` | React/Vite frontend source. Generated `dist/` files are not committed. |
| `auto_research/data/quality/` | Static conference/journal quality tables used only for deterministic metadata and small ranking signals. |
| `tests/` | Backend and workflow regression tests. |
| `requirements.txt` | Python dependencies for the TASTE management environment. |
| `LICENSE` | Package-level license copy. The root README also links the license. |

## Development Entry Points

Run from the repository root:

```bash
PYTHONPATH="$PWD/modules/taste:$PWD:$PWD/scripts" python -m pytest modules/taste/tests -q
npm --prefix modules/taste/auto_research/web/client run build
scripts/start_web.sh
```

TASTE listens on `127.0.0.1:8765` by default. For remote servers, use an SSH tunnel and open the page from your local browser.
