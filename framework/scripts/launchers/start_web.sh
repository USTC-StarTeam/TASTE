#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [[ "${SOURCE_BASHRC:-0}" == "1" && -f "$HOME/.bashrc" ]]; then
  # Optional compatibility path. Normal TASTE launches should rely on explicit PATH/runtime settings.
  set +u
  source "$HOME/.bashrc" || true
  set -u
fi
FRAMEWORK_ROOT="$ROOT/framework"
CLIENT_ROOT="$ROOT/web/frontend/client"
CONDA_EXE="${CONDA_EXE:-$(command -v conda || true)}"
CONDA=""
if [[ -n "$CONDA_EXE" ]]; then
  CONDA="$($CONDA_EXE info --base 2>/dev/null || true)"
fi
ENV_NAME="${CONDA_ENV_NAME:-}"
PORT="${WEB_PORT:-8879}"
HOST="${WEB_HOST:-0.0.0.0}"
FORWARDED_ALLOW_IPS="${WEB_FORWARDED_ALLOW_IPS:-127.0.0.1}"
SSL_CERTFILE="${WEB_SSL_CERTFILE:-}"
SSL_KEYFILE="${WEB_SSL_KEYFILE:-}"

activate_conda_env_if_available() {
  if [[ -z "$ENV_NAME" || -z "$CONDA" || ! -f "$CONDA/etc/profile.d/conda.sh" ]]; then
    return 1
  fi
  set +u
  source "$CONDA/etc/profile.d/conda.sh"
  set -u
  if ! conda env list | awk '{print $1}' | grep -Fxq "$ENV_NAME"; then
    echo "Conda environment '$ENV_NAME' was not found; continuing with the current Python." >&2
    return 1
  fi
  conda activate "$ENV_NAME"
}

choose_python() {
  if [[ -n "${MANAGEMENT_PYTHON:-}" && -x "${MANAGEMENT_PYTHON:-}" ]]; then
    printf '%s\n' "$MANAGEMENT_PYTHON"
  elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    printf '%s\n' "$VIRTUAL_ENV/bin/python"
  elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    echo "python executable not found; set MANAGEMENT_PYTHON or create .venv" >&2
    return 1
  fi
}

# Runtime PATH only. LLM provider/base/model/key are loaded from the saved
# web/project configuration and injected per job; do not create a second config
# source in shell startup or service launch scripts.
NODE_BIN="${NODE_BIN:-}"
if [[ -n "$NODE_BIN" && -d "$NODE_BIN" ]]; then
  export PATH="$NODE_BIN:${PATH}"
fi
if [[ -d "$HOME/.local/bin" ]]; then
  export PATH="$HOME/.local/bin:${PATH}"
fi

if [[ ! -f "$FRAMEWORK_ROOT/scripts/main.py" ]]; then
  echo "missing TASTE entrypoint: $FRAMEWORK_ROOT/scripts/main.py" >&2
  exit 2
fi
if [[ ! -d "$CLIENT_ROOT/dist" ]]; then
  echo "TASTE frontend dist missing; building it first..." >&2
  cd "$CLIENT_ROOT"
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found; install Node.js 20+ or set NODE_BIN to the Node bin directory" >&2
    exit 2
  fi
  if [[ ! -d node_modules ]]; then
    npm install
  fi
  npm run build
fi
cd "$ROOT"

if [[ -z "${MANAGEMENT_PYTHON:-}" && -z "${VIRTUAL_ENV:-}" && ! -x "$ROOT/.venv/bin/python" ]]; then
  activate_conda_env_if_available || true
fi
PYTHON="$(choose_python)"
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$ROOT}"
export PROJECT_ID="${PROJECT_ID:-${DEFAULT_PROJECT_ID:-}}"
export DEFAULT_PROJECT_ID="${DEFAULT_PROJECT_ID:-${PROJECT_ID:-}}"
export MANAGEMENT_PYTHON="${MANAGEMENT_PYTHON:-$PYTHON}"
PY_ROOTS=("$ROOT/framework" "$ROOT/web/backend" "$ROOT" "$ROOT/framework/scripts")
PY_JOINED="$(IFS=:; echo "${PY_ROOTS[*]}")"
export PYTHONPATH="$PY_JOINED${PYTHONPATH:+:$PYTHONPATH}"
UVICORN_ARGS=(
  auto_research.web.server:app
  --host "$HOST"
  --port "$PORT"
  --proxy-headers
  --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
)
if [[ -n "$SSL_CERTFILE" || -n "$SSL_KEYFILE" ]]; then
  if [[ -z "$SSL_CERTFILE" || -z "$SSL_KEYFILE" ]]; then
    echo "WEB_SSL_CERTFILE and WEB_SSL_KEYFILE must be set together" >&2
    exit 2
  fi
  if [[ ! -r "$SSL_CERTFILE" || ! -r "$SSL_KEYFILE" ]]; then
    echo "HTTPS certificate or key is not readable" >&2
    exit 2
  fi
  UVICORN_ARGS+=(--ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE")
fi
exec "$PYTHON" -m uvicorn "${UVICORN_ARGS[@]}"
