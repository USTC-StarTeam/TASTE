#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="${1:-general_research}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT"

printf 'Refreshing session status for project %s...\n' "$PROJECT_NAME"
python3 scripts/init_workspace.py --project "$PROJECT_NAME"
python3 scripts/research_healthcheck.py --project "$PROJECT_NAME"
python3 scripts/report_status.py --project "$PROJECT_NAME"
python3 scripts/detect_machine_profile.py --project "$PROJECT_NAME"
python3 scripts/generate_handoff.py --project "$PROJECT_NAME"

printf '\nNew conversation or standalone runner onboarding order:\n'
printf '1. %s\n' "$ROOT/START_HERE.md"
printf '2. %s\n' "$ROOT/AGENTS.md"
printf '3. %s\n' "$ROOT/projects/$PROJECT_NAME/AGENTS.md"
printf '4. %s\n' "$ROOT/工作状态.txt"
printf '5. %s\n' "$ROOT/automation/three_agent_protocol.md"

printf '\nRecommended first commands:\n'
printf 'cd %s\n' "$ROOT"
printf 'python3 scripts/research_healthcheck.py --project %s\n' "$PROJECT_NAME"
printf 'python3 scripts/report_status.py --project %s\n' "$PROJECT_NAME"
printf 'python3 scripts/detect_machine_profile.py --project %s\n' "$PROJECT_NAME"
printf 'python3 scripts/run_autonomous_research.py --project %s --prompt "<natural-language research goal>" --iterations 1\n' "$PROJECT_NAME"
printf 'python3 scripts/run_loop.py --project %s --prompt "<natural-language research goal>" --iterations 1\n' "$PROJECT_NAME"
printf './scripts/run_in_conda.sh %s python3 scripts/report_status.py --project %s  # optional once a project env exists\n' "$PROJECT_NAME" "$PROJECT_NAME"
