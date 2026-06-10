#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/auto_research/web/client"
npm run build

cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi
exec "$PY" -m uvicorn auto_research.web.server:app --host 127.0.0.1 --port 8765

