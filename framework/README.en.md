# TASTE Python Package Directory

This directory contains only the cross-module TASTE Python framework package and package-level dependency metadata. The FastAPI backend lives in `../web/backend/`, the React/Vite frontend lives in `../web/frontend/client/`, and the seven research stages live under `../modules/`. For user-facing installation, configuration, startup, module usage, acknowledgements, and license notes, use the repository root [README.md](../README.md) as the single source of truth.

Do not copy only `framework/` to run TASTE. The framework needs the full tracked repository, including `modules/`, `web/`, `framework/scripts/`, `modules/*/scripts/`, `framework/resources/templates/`, `framework/resources/prompts/`, `framework/resources/claude/`, and project templates. Concrete research projects, run logs, downloaded repositories, datasets, paper drafts, and local credentials belong to local runtime directories and should not be committed.

## Directory Map

| Path | Purpose |
| --- | --- |
| `auto_research/` | Shared framework code for configuration, storage, Markdown, task boundaries, and common models. |
| `../web/backend/` | FastAPI backend, web job bridge, and project state API. |
| `../web/frontend/client/` | React/Vite frontend source. Generated `dist/` files are not committed. |
| `../modules/` | Seven independently runnable research stages: finding, reading, ideation, planning, environment, experimenting, and writing. |
| `modules/finding/data/quality/` | Static conference/journal quality tables used only for deterministic metadata and small ranking signals. |
| `tests/` | Backend and workflow regression tests. |
| `requirements.txt` | Python dependencies for the TASTE management environment. |
| `LICENSE` | Package-level license copy. The root README also links the license. |

## Development Entry Points

Run from the repository root:

```bash
PYTHONPATH="$PWD/framework:$PWD/web/backend:$PWD/framework/scripts:$PWD/modules/finding:$PWD/modules/reading:$PWD/modules/ideation:$PWD/modules/planning:$PWD/modules/environment:$PWD/modules/experimenting:$PWD/modules/writing:$PWD/modules/finding/scripts:$PWD/modules/reading/scripts:$PWD/modules/ideation/scripts:$PWD/modules/planning/scripts:$PWD/modules/environment/scripts:$PWD/modules/experimenting/scripts:$PWD/modules/writing/scripts:$PWD" python -m pytest tests -q
npm --prefix web/frontend/client run build
framework/scripts/start_web.sh
```

TASTE listens on `127.0.0.1:8765` by default. For remote servers, use an SSH tunnel and open the page from your local browser.
