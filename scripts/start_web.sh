#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${SOURCE_BASHRC:-0}" == "1" && -f "$HOME/.bashrc" ]]; then
  # Optional compatibility path. Normal TASTE launches should rely on explicit PATH/runtime settings.
  set +u
  source "$HOME/.bashrc" || true
  set -u
fi
MODULE_ROOT="$ROOT/modules/taste"
CONDA_EXE="${CONDA_EXE:-$(command -v conda || true)}"
CONDA=""
if [[ -n "$CONDA_EXE" ]]; then
  CONDA="$($CONDA_EXE info --base 2>/dev/null || true)"
fi
ENV_NAME="${CONDA_ENV_NAME:-}"
PORT="${WEB_PORT:-8765}"
HOST="${WEB_HOST:-127.0.0.1}"
API_ONLY="${WEB_API_ONLY:-0}"

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
export LLM_RESPONSE_FORMAT="${LLM_RESPONSE_FORMAT:-json_object}"
export LLM_DISABLE_THINKING="${LLM_DISABLE_THINKING:-0}"
export LLM_PARSE_RETRY_MAX_TOKENS="${LLM_PARSE_RETRY_MAX_TOKENS:-16000}"
export ABSTRACT_SCORING_BATCH_SIZE="${ABSTRACT_SCORING_BATCH_SIZE:-10}"
export ABSTRACT_SCORING_MAX_BATCH_SIZE="${ABSTRACT_SCORING_MAX_BATCH_SIZE:-10}"
export ABSTRACT_SCORING_MAX_WORKERS="${ABSTRACT_SCORING_MAX_WORKERS:-6}"
export ABSTRACT_SCORING_WORKER_CAP="${ABSTRACT_SCORING_WORKER_CAP:-6}"
export ABSTRACT_SCORING_TIMEOUT_SEC="${ABSTRACT_SCORING_TIMEOUT_SEC:-180}"
export ABSTRACT_SCORING_LLM_RETRIES="${ABSTRACT_SCORING_LLM_RETRIES:-2}"
export ABSTRACT_SCORING_WALL_TIMEOUT_SEC="${ABSTRACT_SCORING_WALL_TIMEOUT_SEC:-180}"
export ABSTRACT_SCORING_MAX_TOKENS="${ABSTRACT_SCORING_MAX_TOKENS:-12000}"
export SINGLE_ABSTRACT_SCORING_MAX_TOKENS="${SINGLE_ABSTRACT_SCORING_MAX_TOKENS:-3000}"
export FINAL_LLM_SCORING_LIMIT="${FINAL_LLM_SCORING_LIMIT:-0}"
export OMITTED_ITEM_RETRY_ATTEMPTS="${OMITTED_ITEM_RETRY_ATTEMPTS:-2}"
export ABSTRACT_SCORING_SINGLE_RETRY_ATTEMPTS="${ABSTRACT_SCORING_SINGLE_RETRY_ATTEMPTS:-2}"
export IDEA_TIMEOUT_SEC="${IDEA_TIMEOUT_SEC:-600}"
export IDEA_MAX_TOKENS="${IDEA_MAX_TOKENS:-4000}"
if [[ "${DISABLE_LLM_TITLE_FILTER:-0}" =~ ^(1|true|yes|on)$ ]]; then
  export USE_LLM_TITLE_FILTER="0"
elif [[ "${FORCE_LLM_TITLE_FILTER:-0}" =~ ^(1|true|yes|on)$ ]]; then
  export USE_LLM_TITLE_FILTER="1"
else
  export USE_LLM_TITLE_FILTER="${USE_LLM_TITLE_FILTER:-1}"
fi
export TITLE_FILTER_SEQUENTIAL="${TITLE_FILTER_SEQUENTIAL:-0}"

if [[ ! -d "$MODULE_ROOT" ]]; then
  echo "missing module: $MODULE_ROOT" >&2
  exit 2
fi
if [[ ! -d "$MODULE_ROOT/auto_research/web/client/dist" && "$API_ONLY" =~ ^(1|true|yes|on)$ ]]; then
  echo "TASTE frontend dist missing; WEB_API_ONLY=1 so starting API server without rebuilding the frontend." >&2
elif [[ ! -d "$MODULE_ROOT/auto_research/web/client/dist" ]]; then
  echo "TASTE frontend dist missing; building it first..." >&2
  cd "$MODULE_ROOT/auto_research/web/client"
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found; install Node.js 20+ or set NODE_BIN to the Node bin directory" >&2
    exit 2
  fi
  if [[ ! -d node_modules ]]; then
    npm install
  fi
  npm run build
fi
cd "$MODULE_ROOT"

if [[ -z "${MANAGEMENT_PYTHON:-}" && -z "${VIRTUAL_ENV:-}" && ! -x "$ROOT/.venv/bin/python" ]]; then
  activate_conda_env_if_available || true
fi
PYTHON="$(choose_python)"
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$ROOT}"
export PROJECT_ID="${PROJECT_ID:-${DEFAULT_PROJECT_ID:-}}"
export DEFAULT_PROJECT_ID="${DEFAULT_PROJECT_ID:-${PROJECT_ID:-}}"
export MANAGEMENT_PYTHON="${MANAGEMENT_PYTHON:-$PYTHON}"
export PYTHONPATH="$MODULE_ROOT:$ROOT:$ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m uvicorn auto_research.web.server:app --host "$HOST" --port "$PORT"
