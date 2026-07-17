#!/usr/bin/env bash
set -euo pipefail
if [[ "${SOURCE_BASHRC:-0}" == "1" && -f "$HOME/.bashrc" ]]; then
  # Optional compatibility path. Project commands should normally use explicit
  # runtime/env configuration rather than shell startup files.
  set +u
  source "$HOME/.bashrc" || true
  set -u
fi

PROJECT_NAME="${1:-}"
shift || true
ENV_OVERRIDE=""
ENV_PREFIX_OVERRIDE=""
while [[ "$#" -gt 0 ]]; do
  case "${1:-}" in
    --env-name)
      ENV_OVERRIDE="${2:-}"
      shift 2 || true
      ;;
    --env-prefix)
      ENV_PREFIX_OVERRIDE="${2:-}"
      shift 2 || true
      ;;
    *)
      break
      ;;
  esac
done
if [[ -z "$PROJECT_NAME" || "$#" -eq 0 ]]; then
  echo "usage: framework/scripts/launchers/run_in_conda.sh <project> [--env-name <env>] [--env-prefix <prefix>] <command...>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MANAGEMENT_PYTHON_BIN="${MANAGEMENT_PYTHON:-}"
if [[ -z "$MANAGEMENT_PYTHON_BIN" || ! -x "$MANAGEMENT_PYTHON_BIN" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    MANAGEMENT_PYTHON_BIN="$ROOT/.venv/bin/python"
  else
    MANAGEMENT_PYTHON_BIN="$(command -v python3 || true)"
  fi
fi
if [[ -z "$MANAGEMENT_PYTHON_BIN" || ! -x "$MANAGEMENT_PYTHON_BIN" ]]; then
  echo "management Python not found; set MANAGEMENT_PYTHON" >&2
  exit 1
fi
PROJECT_ROOT="$ROOT/projects/$PROJECT_NAME"
CONFIG="$PROJECT_ROOT/project.json"
if [[ ! -f "$CONFIG" ]]; then
  echo "missing project config: $CONFIG" >&2
  exit 1
fi

CONDA_ENV="$ENV_OVERRIDE"
if [[ -z "$CONDA_ENV" ]]; then
  CONDA_ENV="$("$MANAGEMENT_PYTHON_BIN" - <<'PY2' "$CONFIG"
import json, sys
print(json.load(open(sys.argv[1], 'r', encoding='utf-8')).get('conda_env', ''))
PY2
)"
fi
CONDA_PREFIX_PATH="$ENV_PREFIX_OVERRIDE"
if [[ -z "$CONDA_PREFIX_PATH" ]]; then
  CONDA_PREFIX_PATH="$("$MANAGEMENT_PYTHON_BIN" - <<'PY_PREFIX' "$CONFIG"
import json, sys
cfg = json.load(open(sys.argv[1], 'r', encoding='utf-8'))
runtime = cfg.get('runtime') if isinstance(cfg.get('runtime'), dict) else {}
print(runtime.get('conda_env_prefix', ''))
PY_PREFIX
)"
fi

locate_conda_exe() {
  if [[ -n "${CONDA_EXE:-}" && -x "${CONDA_EXE:-}" ]]; then
    printf '%s\n' "$CONDA_EXE"
    return
  fi
  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return
  fi
  local candidates=(
    "$ROOT/.runtime/miniforge3/bin/conda"
    "$(dirname "$ROOT")/miniforge/bin/conda"
    "$(dirname "$ROOT")/miniforge3/bin/conda"
    "$(dirname "$ROOT")/miniconda3/bin/conda"
    "$HOME/miniforge3/bin/conda"
    "$HOME/miniconda3/bin/conda"
    "$HOME/anaconda3/bin/conda"
    "/opt/conda/bin/conda"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
}

CONDA_EXE_PATH="$(locate_conda_exe || true)"
if [[ -z "$CONDA_EXE_PATH" ]]; then
  echo "unable to locate a conda executable for project $PROJECT_NAME" >&2
  echo "remediation: $MANAGEMENT_PYTHON_BIN $ROOT/framework/scripts/main.py module environment --action deploy_from_plan --project $PROJECT_NAME" >&2
  exit 1
fi

CONDA_BASE="${CONDA_BASE:-}"
if [[ -z "$CONDA_BASE" ]]; then
  CONDA_BASE="$($CONDA_EXE_PATH info --base 2>/dev/null || true)"
fi
if [[ -z "$CONDA_BASE" || ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]]; then
  echo "unable to locate conda base from $CONDA_EXE_PATH" >&2
  exit 1
fi

source "$CONDA_BASE/etc/profile.d/conda.sh"

if [[ -n "$CONDA_PREFIX_PATH" ]]; then
  if [[ ! -x "$CONDA_PREFIX_PATH/bin/python" ]]; then
    echo "configured Environment handoff prefix is not runnable: $CONDA_PREFIX_PATH" >&2
    echo "remediation: $MANAGEMENT_PYTHON_BIN $ROOT/framework/scripts/main.py module environment --action deploy_from_plan --project $PROJECT_NAME" >&2
    exit 2
  fi
  cd "$ROOT"
  exec "$CONDA_EXE_PATH" run -p "$CONDA_PREFIX_PATH" "$@"
fi

if [[ -n "$CONDA_ENV" && "$CONDA_ENV" != "base" ]]; then
  ENV_JSON="$($CONDA_EXE_PATH env list --json 2>/dev/null || true)"
  ENV_EXISTS="$("$MANAGEMENT_PYTHON_BIN" - <<'PY3' "$CONDA_ENV" "$ENV_JSON"
import json, sys
name = sys.argv[1]
raw = sys.argv[2]
try:
    payload = json.loads(raw)
except Exception:
    print('no')
    raise SystemExit(0)
print('yes' if any(path.rstrip('/').split('/')[-1] == name for path in payload.get('envs', [])) else 'no')
PY3
)"
  if [[ "$ENV_EXISTS" != "yes" ]]; then
    echo "conda environment '$CONDA_ENV' does not exist for project $PROJECT_NAME" >&2
    echo "remediation: $MANAGEMENT_PYTHON_BIN $ROOT/framework/scripts/main.py module environment --action deploy_from_plan --project $PROJECT_NAME" >&2
    exit 1
  fi
fi

if [[ -z "$CONDA_ENV" || "$CONDA_ENV" == "base" ]]; then
  if [[ "${ALLOW_CONDA_BASE:-0}" != "1" ]]; then
    echo "refusing to run project command in conda base/empty env for project $PROJECT_NAME" >&2
    echo "set a project conda_env or pass --env-name <env>; use ALLOW_CONDA_BASE=1 only for explicit diagnostics" >&2
    exit 2
  fi
fi

if [[ -n "$CONDA_ENV" ]]; then
  conda activate "$CONDA_ENV"
fi
cd "$ROOT"
exec "$@"
