#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [[ "${SOURCE_BASHRC:-0}" == "1" && -f "$HOME/.bashrc" ]]; then
  if [[ "${TASTE_BASHRC_ENV_LOADED:-0}" != "1" ]]; then
    # A normal non-interactive `source ~/.bashrc` returns at the standard
    # interactive-shell guard. Re-enter once through interactive Bash so the
    # complete exported environment is inherited by this service process.
    export TASTE_BASHRC_ENV_LOADED=1
    exec bash -ic 'exec "$@"' taste-start-web "$0" "$@"
  fi
fi
unset TASTE_BASHRC_ENV_LOADED
FRAMEWORK_ROOT="$ROOT/framework"
CLIENT_ROOT="$ROOT/web/frontend/client"
MANAGEMENT_PYTHON_BIN="${MANAGEMENT_PYTHON:-}"
MANAGEMENT_CONDA_PREFIX=""
if [[ -n "$MANAGEMENT_PYTHON_BIN" && -x "$MANAGEMENT_PYTHON_BIN" ]]; then
  MANAGEMENT_CONDA_PREFIX="$($MANAGEMENT_PYTHON_BIN -c '
from pathlib import Path
import sys

prefix = Path(sys.prefix).expanduser().resolve()
print(prefix if prefix.parent.name == "envs" else "")
' 2>/dev/null || true)"
fi
ENV_NAME="${CONDA_ENV_NAME:-}"
if [[ -z "$ENV_NAME" && -n "$MANAGEMENT_CONDA_PREFIX" ]]; then
  ENV_NAME="$(basename "$MANAGEMENT_CONDA_PREFIX")"
fi
PORT="${WEB_PORT:-8879}"
HOST="${WEB_HOST:-0.0.0.0}"
FORWARDED_ALLOW_IPS="${WEB_FORWARDED_ALLOW_IPS:-127.0.0.1}"
SSL_CERTFILE="${WEB_SSL_CERTFILE:-}"
SSL_KEYFILE="${WEB_SSL_KEYFILE:-}"

locate_conda_exe() {
  if [[ -n "${CONDA_EXE:-}" && -x "${CONDA_EXE:-}" ]]; then
    printf '%s\n' "$CONDA_EXE"
    return
  fi
  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return
  fi
  local candidate
  local candidates=()
  if [[ -n "$MANAGEMENT_CONDA_PREFIX" ]]; then
    candidates+=("$(dirname "$(dirname "$MANAGEMENT_CONDA_PREFIX")")/bin/conda")
  fi
  candidates+=(
    "$HOME/miniforge3/bin/conda"
    "$HOME/miniconda3/bin/conda"
    "$HOME/anaconda3/bin/conda"
    "/opt/conda/bin/conda"
  )
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
}

activate_management_conda_env() {
  local target="${MANAGEMENT_CONDA_PREFIX:-$ENV_NAME}"
  if [[ -z "$target" ]]; then
    return 0
  fi
  local conda_exe
  local conda_base
  conda_exe="$(locate_conda_exe || true)"
  if [[ -z "$conda_exe" ]]; then
    echo "unable to locate conda while activating TASTE management environment '$target'" >&2
    return 1
  fi
  conda_base="$($conda_exe info --base 2>/dev/null || true)"
  if [[ -z "$conda_base" || ! -f "$conda_base/etc/profile.d/conda.sh" ]]; then
    echo "unable to locate conda initialization from $conda_exe" >&2
    return 1
  fi
  set +u
  source "$conda_base/etc/profile.d/conda.sh"
  set -u
  if ! conda activate "$target"; then
    echo "unable to activate TASTE management environment '$target'" >&2
    return 1
  fi
  if [[ -n "$MANAGEMENT_CONDA_PREFIX" ]]; then
    local expected_prefix
    local active_prefix
    expected_prefix="$(readlink -f "$MANAGEMENT_CONDA_PREFIX")"
    if [[ -z "${CONDA_PREFIX:-}" ]]; then
      echo "conda activation did not set CONDA_PREFIX for '$target'" >&2
      return 1
    fi
    active_prefix="$(readlink -f "$CONDA_PREFIX")"
    if [[ -z "$active_prefix" || "$active_prefix" != "$expected_prefix" ]]; then
      echo "activated conda prefix '$active_prefix' does not match management Python prefix '$expected_prefix'" >&2
      return 1
    fi
  fi
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

activate_management_conda_env
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
