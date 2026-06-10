#!/usr/bin/env python3
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import json
import os
import re
import selectors
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_state import append_agent_log, consume_guidance, mark_agent, upsert_agent
from runtime_env import find_binary, interactive_env
from project_paths import ROOT, build_paths, load_project_config, management_python, project_experiment_python_from_config
from guard_selected_base_route import repair_project as guard_selected_base_route
from run_project import current_find_execution_contract

NATIVE_SKILL_NAMES = {"experiment-loop", "evidence-gate", "writing"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*[\"']?)[A-Za-z0-9._\-]+"),
]


def redact_secrets(value: Any) -> str:
    text = str(value or "")
    for pattern in SECRET_PATTERNS:
        def repl(match):
            if match.lastindex:
                return match.group(1) + "[REDACTED]"
            token = match.group(0)
            return token[:6] + "...[REDACTED]"
        text = pattern.sub(repl, text)
    return text

NATIVE_SKILL_LABELS = {
    "experiment-loop": "TASTE experiment-loop contract",
    "evidence-gate": "TASTE evidence-assurance contract",
    "writing": "writing contract",
}
CURRENT_FIND_ARTIFACT_WRITER_POLICY = (
    "current-Find Read/Idea/Plan JSON artifacts and deep-read fragments must not be generated or patched by Bash/Python/cat/heredoc; "
    "Claude file tools may author or repair individual deep-read fragment JSON files, while read_results.json, ideas.json, and plans.json must be complete Claude Write artifacts"
)
CURRENT_FIND_JSON_WRITE_ONLY_POLICY = (
    "current-Find JSON artifacts must be written with one complete Claude Write call; "
    "Edit/MultiEdit on read_results.json, ideas.json, or plans.json is blocked because it can leave invalid partial JSON. "
    "If one field needs repair, rewrite the entire JSON artifact with Claude Write."
)
CURRENT_FIND_SELECTION_ONLY_POLICY = (
    "current-Find selection-only stage may only Write planning/finding/plans.json once; "
    "read_results.json, ideas.json, deep-read fragments, Markdown projections, and TASTE-owned state remain read-only"
)
CURRENT_FIND_MARKDOWN_OWNED_POLICY = (
    "current-Find Markdown artifacts are rendered by the wrapper from validated JSON; "
    "Claude must not write read.md, idea.md, or plan.md directly in current-Find Read/Idea/Plan"
)
CURRENT_FIND_GATE_STATE_WRITER_POLICY = (
    "TASTE-owned current-Find gate/state files are read-only for Claude in current-Find Read/Idea/Plan; "
    "the wrapper writes state/current_find_research_plan.json, state/idea_candidates.json, and "
    "state/experiment_plan.json only after machine validation passes or blocks"
)
CURRENT_FIND_CONTENT_ARTIFACTS = [
    "planning/finding/read_results.json",
    "planning/finding/read.md",
    "planning/finding/ideas.json",
    "planning/finding/idea.md",
    "planning/finding/plans.json",
    "planning/finding/plan.md",
    "planning/finding/current_find_deep_read_fragments",
]
CURRENT_FIND_DEEP_READ_FRAGMENT_DIR = "planning/finding/current_find_deep_read_fragments"
CURRENT_FIND_JSON_ARTIFACTS = [
    "planning/finding/read_results.json",
    "planning/finding/ideas.json",
    "planning/finding/plans.json",
]
CURRENT_FIND_MARKDOWN_ARTIFACTS = [
    "planning/finding/read.md",
    "planning/finding/idea.md",
    "planning/finding/plan.md",
]
CURRENT_FIND_OWNED_STATE_FILES = [
    "state/current_find_research_plan.json",
    "state/idea_candidates.json",
    "state/experiment_plan.json",
]
CURRENT_FIND_CONTROLLED_FILE_NAMES = [
    "read_results.json", "read.md", "ideas.json", "idea.md", "plans.json", "plan.md",
    "current_find_research_plan.json", "idea_candidates.json", "experiment_plan.json",
]


def is_current_find_artifact_policy_reason(reason: Any) -> bool:
    text = str(reason or "")
    return (
        "current-Find Read/Idea/Plan artifacts" in text
        or "current-Find Read/Idea/Plan JSON artifacts" in text
        or "current-Find JSON artifacts" in text
        or "current-Find Markdown artifacts" in text
        or "current-Find selection-only stage" in text
    )


def is_current_find_gate_state_policy_reason(reason: Any) -> bool:
    return "TASTE-owned current-Find gate/state files" in str(reason or "")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        skip_key = chr(95) + chr(112) + chr(97) + chr(116) + chr(104) + chr(115)
        return {str(k): json_safe(v) for k, v in value.items() if str(k) != skip_key}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, ensure_ascii=False) + chr(10), encoding=chr(117) + chr(116) + chr(102) + chr(45) + chr(56))

def read_text(path: Path, limit: int = 12000) -> str:
    if not path.exists() or not path.is_file():
        return ''
    return path.read_text(encoding='utf-8', errors='replace')[:limit]


def load_recoverable_cycle(paths) -> Any:
    legacy_prefix = "evo" + "scientist"
    for name in ('recoverable_cycle_summary.json', f'{legacy_prefix}_cycle_summary.json', f'{legacy_prefix}_style_cycle.json'):
        payload = load_json(paths.state / name, {})
        if payload:
            return payload
    return {}


def local_skill_files() -> list[Path]:
    skill_root = ROOT / ".claude" / "skills"
    if not skill_root.exists():
        return []
    return sorted(path for path in skill_root.glob("*/SKILL.md") if path.parent.name in NATIVE_SKILL_NAMES)


def skill_contract_summary(limit: int = 12000) -> str:
    rows = []
    for path in local_skill_files():
        text = read_text(path, 900)
        first_lines = [line.strip() for line in text.splitlines() if line.strip()]
        description = ""
        for line in first_lines:
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
        label = NATIVE_SKILL_LABELS.get(path.parent.name, path.parent.name)
        rows.append(f"- {label}: {description or first_lines[0] if first_lines else 'skill contract'}")
    return "\n".join(rows)[:limit]


def find_claude(cfg: dict[str, Any]) -> str:
    return find_binary('claude', cfg=cfg) or 'claude'


def existing_path(value: Any) -> str:
    if not value:
        return ''
    try:
        path = Path(str(value)).expanduser().resolve()
        return str(path) if path.exists() else ''
    except Exception:
        return ''

def project_experiment_python(project: str) -> str:
    paths = build_paths(project)
    cfg = load_json(paths.config, {})
    return project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {})


def allowed_experiment_pythons(project: str) -> set[str]:
    expected = project_experiment_python(project)
    if not expected:
        return set()
    expected_path = Path(expected).resolve()
    out = {str(expected_path)}
    for name in ['python3', 'python3.11', 'python3.10', 'python3.9']:
        candidate = expected_path.parent / name
        if candidate.exists():
            out.add(str(candidate.resolve()))
    return out


def command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except Exception:
        return str(command or '').split()


def resolve_executable_token(token: str, cwd: str = '') -> str:
    if not token:
        return ''
    candidate = Path(token).expanduser()
    if candidate.is_absolute():
        try:
            return str(candidate.resolve())
        except Exception:
            return str(candidate)
    if cwd:
        local = (Path(cwd) / candidate).resolve()
        if local.exists():
            return str(local)
    return token


def launcher_training_argv(tokens: list[str]) -> list[str]:
    for index, token in enumerate(tokens):
        if token == '--':
            return tokens[index + 1:]
    return []


def launcher_training_python_issue(command: str, project: str) -> str:
    tokens = command_tokens(command)
    if not any('launch_experiment_run.py' in token for token in tokens):
        return ''
    if '--allow-nonproject-python' in tokens:
        return 'launcher command uses forbidden --allow-nonproject-python escape hatch for autonomous experiment launch'
    training = launcher_training_argv(tokens)
    if not training:
        return ''
    launcher = Path(training[0]).name.lower()
    if launcher in {'conda', 'mamba', 'micromamba'} and 'run' in [item.lower() for item in training[:4]]:
        return 'launcher training argv uses conda/mamba run; use the project experiment Python executable directly'
    first = resolve_executable_token(training[0])
    first_name = Path(first).name.lower()
    if not (first_name.startswith('python') or str(training[0]).endswith('.py')):
        return ''
    allowed = allowed_experiment_pythons(project)
    if not allowed:
        return 'project experiment Python could not be resolved from project config'
    resolved = resolve_executable_token(training[0])
    if Path(resolved).exists():
        resolved = str(Path(resolved).resolve())
    if resolved not in allowed:
        return f'launcher training argv must use project experiment Python; expected one of {sorted(allowed)}, got {training[0]!r}'
    return ''



def strip_shell_heredoc_payload(command: str) -> str:
    """Return executable shell text with here-doc bodies removed."""
    lines = str(command or '').splitlines()
    if not lines:
        return ''
    visible: list[str] = []
    delimiter = ''
    pattern = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
    for line in lines:
        if delimiter:
            if line.strip() == delimiter:
                delimiter = ''
            continue
        visible.append(line)
        match = pattern.search(line)
        if match:
            delimiter = match.group(1)
    return '\n'.join(visible)


def is_read_only_log_monitor_command(command: str, lowered: str | None = None) -> bool:
    """Allow Claude to watch existing research logs without classifying it as a launch."""
    text = str(command or '').strip()
    if not text:
        return False
    lower = lowered if lowered is not None else text.lower()
    if not ('.log' in lower or 'stdout_stderr.log' in lower):
        return False
    forbidden_markers = [
        'python ', 'python3', '/bin/python', 'conda run', 'mamba run', 'micromamba run',
        'torchrun', 'accelerate ', 'deepspeed', 'nohup', 'tmux ', 'screen ', 'setsid ',
        'launch_experiment_run.py', ' train_', ' train-', ' train.', ' finetune', ' main.py',
    ]
    if any(marker in lower for marker in forbidden_markers):
        return False
    if '`' in text or '$(' in text:
        return False
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        for chunk in re.split(r"\s*(?:&&|\|\||;)\s*", line):
            for part in re.split(r"(?<![>&])&(?!&)", chunk):
                part = part.strip()
                if part:
                    parts.append(part)
    if not parts:
        return False
    for part in parts:
        cleaned = re.sub(r"\s+[12]?>&?\s*/dev/null\b", "", part).strip()
        if re.search(r"(?<![12])>|<", cleaned):
            return False
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\$!?$", cleaned):
            continue
        if re.match(r"^sleep\s+[0-9]+(?:\.[0-9]+)?$", cleaned):
            continue
        if re.match(r"^kill\s+\$[A-Za-z_][A-Za-z0-9_]*(?:\s+2>/dev/null)?$", part.strip()):
            continue
        if cleaned.startswith('tail ') and re.search(r"(?:^|\s)-?(?:f|n|[0-9])", cleaned) and '.log' in cleaned:
            continue
        return False
    return True


def _current_find_stage(stage: str = '') -> bool:
    stage_l = str(stage or '').lower().replace('_', '-')
    return 'current-find' in stage_l or 'read-idea-plan' in stage_l


def _current_find_selection_stage(stage: str = '') -> bool:
    stage_l = str(stage or '').lower().replace('_', '-')
    return 'current-find-claude-select-plan' in stage_l or 'current-find-selection' in stage_l


def claude_no_event_timeout_seconds(stage: str, effective_timeout: int, env: dict[str, Any] | None = None, coding_cfg: dict[str, Any] | None = None) -> int:
    env = env if isinstance(env, dict) else {}
    coding_cfg = coding_cfg if isinstance(coding_cfg, dict) else {}
    try:
        value = int(env.get("CLAUDE_NO_EVENT_TIMEOUT_SEC") or coding_cfg.get("claude_no_event_timeout_sec") or 300)
    except Exception:
        value = 300
    value = max(60, value)
    if _current_find_stage(stage):
        floor = 1800
        if effective_timeout and effective_timeout > 0:
            floor = min(floor, max(60, int(effective_timeout) - 60))
        value = max(value, floor)
    return value


def _mentions_path(text: Any, paths: list[str]) -> bool:
    lowered = str(text or '').replace('\\', '/').lower()
    return any(path.lower() in lowered for path in paths)


def _shell_redirects_to_current_find_file(command: str) -> bool:
    text = str(command or '').replace('\\', '/')
    controlled = {name.lower() for name in CURRENT_FIND_CONTROLLED_FILE_NAMES}

    def is_controlled_target(target: str) -> bool:
        cleaned = target.strip().strip('"\'')
        if not cleaned or cleaned.startswith('&'):
            return False
        lowered = cleaned.lower()
        if Path(cleaned).name.lower() in controlled:
            return True
        return CURRENT_FIND_DEEP_READ_FRAGMENT_DIR.lower() in lowered

    for match in re.finditer(r"(?:^|\s)(?:[12])?(?:>>|>)\s*([^\s;&|]+)", text):
        if is_controlled_target(match.group(1)):
            return True
    for match in re.finditer(r"(?:^|\s)(?:tee|tee\s+-a)\s+([^\s;&|]+)", text, re.I):
        if is_controlled_target(match.group(1)):
            return True
    return False


def _python_opens_current_find_file_for_write(command: str, names: list[str]) -> bool:
    lowered = str(command or '').replace('\\', '/').lower()
    target_group = "|".join(re.escape(Path(name).name.lower()) for name in names)
    return bool(re.search(
        rf"open\s*\([^\n)]*(?:{target_group})[^\n)]*['\"](?:w|a|x|\+)",
        lowered,
        re.S,
    ))



def current_find_artifact_generator_policy_issue(command: str, stage: str = '') -> str:
    if not _current_find_stage(stage):
        return ''
    raw = str(command or '')
    lowered = raw.lower()
    content_target = _mentions_path(lowered, CURRENT_FIND_CONTENT_ARTIFACTS)
    gate_state_target = _mentions_path(lowered, CURRENT_FIND_OWNED_STATE_FILES)
    writer_markers = ['write_text(', 'json.dump(', '.write(']
    shell_writer = any(marker in lowered for marker in ['python', '<<', 'cat ', 'tee ', 'write_text', 'json.dump', '.write(', 'open('])
    writes_target = any(marker in lowered for marker in writer_markers)
    open_write_target = _python_opens_current_find_file_for_write(raw, CURRENT_FIND_OWNED_STATE_FILES)
    content_open_write_target = _python_opens_current_find_file_for_write(raw, CURRENT_FIND_CONTENT_ARTIFACTS)
    redirect_target = _shell_redirects_to_current_find_file(raw)
    tmp_generator = bool(
        re.search(r">\s*/tmp/(?:gen|build|make)_[^\s;&|]*(?:read_results|read|ideas|idea|plans|plan)[^\s;&|]*\.py", lowered)
        and re.search(r"(?:python3?|/[^\s;&|]*/python)\s+/tmp/(?:gen|build|make)_[^\s;&|]*(?:read_results|read|ideas|idea|plans|plan)[^\s;&|]*\.py", lowered)
    )
    fragment_target = CURRENT_FIND_DEEP_READ_FRAGMENT_DIR.lower() in lowered
    fragment_python_writer = fragment_target and any(marker in lowered for marker in writer_markers)
    fragment_script_execution = bool(re.search(r"(?:python3?|/[^\s;&|]*/python)\s+[^\s;&|]*current_find_deep_read_fragments/[^\s;&|]+\.(?:py|sh)\b", lowered))
    if gate_state_target and (writes_target or open_write_target or redirect_target):
        return CURRENT_FIND_GATE_STATE_WRITER_POLICY
    if content_target and (content_open_write_target or redirect_target or tmp_generator or fragment_python_writer or fragment_script_execution) and shell_writer:
        return CURRENT_FIND_ARTIFACT_WRITER_POLICY
    return ''


def current_find_tool_policy_issue(name: str, tool_input: Any, stage: str = '') -> str:
    if not _current_find_stage(stage):
        return ''
    label = str(name or '')
    if label not in {'Write', 'Edit', 'MultiEdit'}:
        return ''
    data = tool_input if isinstance(tool_input, dict) else {}
    target = str(data.get('file_path') or data.get('file') or data.get('path') or data.get('filename') or tool_input or '')
    if _mentions_path(target, CURRENT_FIND_OWNED_STATE_FILES):
        return CURRENT_FIND_GATE_STATE_WRITER_POLICY
    if _mentions_path(target, CURRENT_FIND_MARKDOWN_ARTIFACTS):
        return CURRENT_FIND_MARKDOWN_OWNED_POLICY
    fragment_target = _mentions_path(target, [CURRENT_FIND_DEEP_READ_FRAGMENT_DIR])
    if fragment_target:
        lowered_target = target.replace("\\", "/").lower()
        if label not in {"Write", "Edit", "MultiEdit"} or not lowered_target.endswith(".json"):
            return CURRENT_FIND_ARTIFACT_WRITER_POLICY
        if _current_find_selection_stage(stage):
            return CURRENT_FIND_SELECTION_ONLY_POLICY
        return ""
    if _current_find_selection_stage(stage):
        if label == "Write" and _mentions_path(target, ["planning/finding/plans.json"]):
            return ""
        if _mentions_path(target, CURRENT_FIND_JSON_ARTIFACTS):
            return CURRENT_FIND_SELECTION_ONLY_POLICY
    if label in {'Edit', 'MultiEdit'} and _mentions_path(target, CURRENT_FIND_JSON_ARTIFACTS):
        return CURRENT_FIND_JSON_WRITE_ONLY_POLICY
    return ''

def bash_command_tool_policy_issue(command: str, project: str, stage: str = '') -> str:
    current_find_issue = current_find_artifact_generator_policy_issue(command, stage)
    if current_find_issue:
        return current_find_issue
    policy_command = strip_shell_heredoc_payload(command)
    lowered = policy_command.lower()
    if not lowered.strip():
        return ''
    if 'run_research_trajectory_supervisor.py' in lowered:
        return 'trajectory supervisor recursion is blocked: Claude Code workers must complete the assigned queue item instead of spawning nested supervisors'
    launcher_issue = launcher_training_python_issue(policy_command, project)
    if launcher_issue:
        return launcher_issue
    management_script = re.search(r"(?:^|[;&|]\s*)(?:python|python3|python3\.\d+)\s+scripts/(?!launch_experiment_run\.py)[a-zA-Z0-9_./-]+\.py\b", lowered)
    if management_script:
        return f'Management scripts must use the configured management Python ({management_python()}), not bare python/python3'
    if 'scripts/launch_experiment_run.py' in lowered or 'launch_experiment_run.py' in lowered:
        return ''
    if is_read_only_log_monitor_command(policy_command, lowered):
        return ''
    inline_python_probe = bool(
        re.search(r"(?:python(?:3)?|conda\s+run\b.*?python(?:3)?)(?=[^;&|]*\s-c\b)", lowered)
        or re.search(r"(?:python(?:3)?|conda\s+run\b.*?python(?:3)?)(?=[^;&|]*<<)", lowered)
    )
    inline_training_markers = [
        'loss.backward', '.backward(', 'optimizer.step', 'model.train(',
        'for epoch', 'range(args.epoch', 'range(epoch', ' train(', '.fit(',
        'save_pretrained', '--artifact_dir',
    ]
    if inline_python_probe and not any(marker in lowered for marker in inline_training_markers):
        return ''
    training_script_pattern = re.compile(r"(?:python(?:3)?|conda\s+run\b.*?python(?:3)?)\s+[^;&|]*?(?:finetune[\w.-]*\.py|exp_[A-Za-z0-9_\-]+\.py|exp_standalone[A-Za-z0-9_\-]*\.py|main\.py|train[\w.-]*\.py)\b")
    if training_script_pattern.search(lowered) and re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", lowered):
        return ''
    explicit_artifact_log = bool(re.search(r"(?:>|>>)\s*\S*(?:/artifacts/|stdout_stderr\.log|output\.log)", lowered))
    shell_background_operator = bool(re.search(r"(?<![>&|])&(?!&)", lowered))
    background_launch = (
        any(marker in lowered for marker in ['nohup', 'tmux ', 'screen ', 'setsid '])
        or shell_background_operator
    )
    invokes_training = bool(training_script_pattern.search(lowered))
    if invokes_training:
        return 'new experiment launch bypasses scripts/launch_experiment_run.py launcher contract'
    if background_launch and any(marker in lowered for marker in ['finetune', 'exp_standalone', 'exp_text_init', '/artifacts/', '--artifact_dir']):
        return 'new experiment launch bypasses scripts/launch_experiment_run.py launcher contract'
    if explicit_artifact_log and any(marker in lowered for marker in ['finetune', 'exp_standalone', 'exp_text_init', '--artifact_dir']):
        return 'new experiment launch bypasses scripts/launch_experiment_run.py launcher contract'
    return ''

def current_find_run_id(paths) -> str:
    for rel in [paths.planning / 'finding' / 'find_results.json', paths.state / 'current_find_research_plan.json']:
        payload = load_json(rel, {})
        if isinstance(payload, dict):
            run_id = str(payload.get('run_id') or payload.get('find_run_id') or '').strip()
            if run_id:
                return run_id
    return ''



def title_key_for_current_find(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_recommended_title_keys(paths_or_root) -> set[str]:
    payload = load_json(paths_or_root.planning / "finding" / "find_results.json", {})
    if not isinstance(payload, dict):
        return set()
    keys: set[str] = set()
    for pool in ["articles", "strong_recommendations"]:
        rows = payload.get(pool)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                key = title_key_for_current_find(row.get("title") or row.get("paper_title"))
                if key:
                    keys.add(key)
    return keys


def selected_title_in_current_find(paths_or_root, selected: dict[str, Any], decision: dict[str, Any] | None = None) -> bool:
    decision = decision if isinstance(decision, dict) else {}
    title = selected.get("title") or selected.get("literature_base_title") or selected.get("selected_base_title") or decision.get("selected_base_title") or selected.get("name") or ""
    key = title_key_for_current_find(title)
    if key and key in current_find_recommended_title_keys(paths_or_root):
        return True
    root = Path(paths_or_root.root) if hasattr(paths_or_root, "root") else Path(paths_or_root)
    audit = load_json(root / "state" / "fresh_base_reference_reproduction_audit.json", {})
    audit_selected = audit.get("selected_base") if isinstance(audit, dict) and isinstance(audit.get("selected_base"), dict) else {}
    selected_repo = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    audit_repo = str(audit.get("repo_path") or audit.get("active_repo_path") or audit_selected.get("repo_path") or audit_selected.get("local_path") or "").strip() if isinstance(audit, dict) else ""
    audit_title = audit_selected.get("literature_base_title") or audit_selected.get("title") or audit.get("paper_title") or audit.get("base_title") or "" if isinstance(audit, dict) else ""
    audit_run = str(audit_selected.get("fresh_find_run_id") or "").strip()
    selected_run = str(selected.get("fresh_find_run_id") or "").strip()
    if selected_repo and audit_repo and selected_repo == audit_repo and (
        (audit_run and selected_run == audit_run)
        or (key and title_key_for_current_find(audit_title) == key)
    ):
        return True
    gate = load_json(root / "state" / "base_switch_gate.json", {})
    execution = load_json(root / "state" / "base_switch_execution.json", {})
    candidate = gate.get("candidate_route") if isinstance(gate, dict) and isinstance(gate.get("candidate_route"), dict) else {}
    candidate_repo = str(candidate.get("repo_path") or "").strip()
    return bool(
        selected_repo
        and candidate_repo
        and selected_repo == candidate_repo
        and isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )

def current_environment_selection(paths) -> dict[str, Any]:
    run_id = current_find_run_id(paths)
    selection = load_json(paths.state / 'evidence_ready_repo_selection.json', {})
    if not isinstance(selection, dict):
        return {'valid': False, 'current_find_run_id': run_id, 'reason': 'missing_evidence_ready_repo_selection'}
    selected = selection.get('selected', {}) if isinstance(selection.get('selected'), dict) else {}
    selected_run = str(selected.get('fresh_find_run_id') or selection.get('fresh_find_run_id') or '').strip()
    stage = str(selection.get('selection_stage') or selection.get('selected_by_stage') or '').strip()
    decision = selection.get('claude_topic_decision') if isinstance(selection.get('claude_topic_decision'), dict) else {}
    accepted = bool(selection.get('accepted_by_claude') or str(selection.get('selection_gate') or '').startswith(('accepted_by_claude', 'accepted_by_deterministic_base_switch_gate')) or decision.get('accept_as_current_best'))
    in_current_find = selected_title_in_current_find(paths, selected, decision)
    valid = bool(selected and stage == 'environment_claude_code' and accepted and in_current_find)
    return {'valid': valid, 'current_find_run_id': run_id, 'fresh_find_run_id': selected_run, 'selection_stage': stage, 'accepted_by_claude': accepted, 'selected': selected, 'in_current_find_recommendations': in_current_find, 'reason': 'current_environment_base_selected' if valid else ('selected_base_not_in_current_find_recommendations' if not in_current_find else 'environment_base_selection_pending_or_stale')}


def stage_allows_selected_repo(stage: str = '') -> bool:
    stage_l = str(stage or '').lower().replace('_', '-')
    if 'current-find' in stage_l or 'read-idea-plan' in stage_l:
        return False
    if stage_l.startswith('full-cycle-'):
        return True
    return any(token in stage_l for token in ['environment', 'fresh-base', 'implementation', 'experiment', 'reference', 'smoke', 'reproduction', 'paper'])


def active_repo_path(paths) -> str:
    env = current_environment_selection(paths)
    if not env.get('valid'):
        return ''
    selected = env.get('selected', {}) if isinstance(env.get('selected'), dict) else {}
    direct = existing_path(selected.get('repo_path') or selected.get('local_path') or selected.get('path'))
    if direct:
        return direct
    active = load_json(paths.state / 'active_repo.json', {})
    if isinstance(active, dict):
        active_run = str(active.get('selected_by') or active.get('fresh_find_run_id') or '')
        active_stage = str(active.get('selection_stage') or active.get('selected_by_stage') or '')
        if active_run == env.get('current_find_run_id') and active_stage == 'environment_claude_code':
            return existing_path(active.get('repo_path') or active.get('local_path') or active.get('path'))
    return ''


def fresh_base_repo_path(paths) -> str:
    env = current_environment_selection(paths)
    if not env.get('valid'):
        return ''
    # Current environment-stage selection is authoritative. Implementation
    # plans may retain legacy/proposal repos for audit and must not drive the
    # persistent Claude working directory.
    current = active_repo_path(paths)
    if current:
        return current
    selected = env.get('selected', {}) if isinstance(env.get('selected'), dict) else {}
    direct = existing_path(selected.get('repo_path') or selected.get('local_path') or selected.get('path'))
    if direct:
        return direct
    plan = load_json(paths.state / 'fresh_base_implementation_plan.json', {})
    repo = plan.get('repo', {}) if isinstance(plan, dict) and isinstance(plan.get('repo', {}), dict) else {}
    return existing_path(repo.get('repo_path') or repo.get('local_path') or repo.get('path'))

def fresh_base_route_active(paths, stage: str = '') -> bool:
    return bool(current_environment_selection(paths).get('valid') and stage_allows_selected_repo(stage))


def resolve_session_repo_path(paths, stage: str = '', repo_path: str = '') -> str:
    explicit = existing_path(repo_path)
    if explicit and stage_allows_selected_repo(stage):
        return explicit
    if not stage_allows_selected_repo(stage):
        return ''
    fresh_repo = fresh_base_repo_path(paths)
    if fresh_repo and fresh_base_route_active(paths, stage):
        return fresh_repo
    return active_repo_path(paths)


def safe_session_key(value: str = '') -> str:
    key = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(value or '').strip())
    return key.strip('._-')[:80] or 'main'


def session_key_for(agent_id: str = 'main', stage: str = '') -> str:
    agent = safe_session_key(agent_id or 'main')
    stage_key = safe_session_key(stage or '')
    if agent == 'main' and stage_key in {'environment', 'experiment', 'paper'}:
        return stage_key
    if agent == 'main' and not stage_key.startswith('writing') and 'paper' not in stage_key:
        return 'main'
    if agent and agent != 'main':
        return agent
    return stage_key or 'main'


def keyed_state_path(paths, stem: str, session_key: str = 'main', suffix: str = '.json') -> Path:
    key = safe_session_key(session_key)
    if key == 'main':
        return paths.state / f'{stem}{suffix}'
    return paths.state / f'{stem}_{key}{suffix}'


def session_path(paths, session_key: str = 'main') -> Path:
    return keyed_state_path(paths, 'claude_project_session', session_key)


def history_path(paths, session_key: str = 'main') -> Path:
    key = safe_session_key(session_key)
    if key == 'main':
        return paths.reports / 'claude_project_session.md'
    return paths.reports / f'claude_project_session_{key}.md'


FRESH_SESSION_STAGES = {"current-find-claude-read-idea-plan", "current-find-claude-select-plan"}
FRESH_SESSION_STAGE_PREFIXES = ("full-cycle-blocker-repair",)


def previous_context_overflow(paths, session_key: str = 'main') -> bool:
    last = load_json(keyed_state_path(paths, 'claude_project_session_last_result', session_key), {})
    if not isinstance(last, dict):
        return False
    haystack = "\n".join(str(last.get(key) or "") for key in ['stdout', 'raw_stdout', 'stderr'])
    lowered = haystack.lower()
    return 'maximum context length' in lowered or ('context length' in lowered and 'requested' in lowered)


def fresh_session_reason(stage: str, paths, session_key: str = 'main') -> str:
    stage_key = str(stage or '').strip().lower().replace('_', '-')
    if stage_key in FRESH_SESSION_STAGES:
        return 'stage_requires_fresh_context'
    if any(stage_key.startswith(prefix) for prefix in FRESH_SESSION_STAGE_PREFIXES):
        return 'blocker_repair_uses_compact_fresh_context'
    if previous_context_overflow(paths, session_key=session_key):
        return 'previous_claude_context_overflow'
    return ''


def ensure_session(project: str, repo_path: str = '', session_key: str = 'main') -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    existing = load_json(session_path(paths, session_key), {})
    if not isinstance(existing, dict):
        existing = {}
    repo = existing_path(repo_path)
    previous_repo = existing_path(existing.get('repo_path'))
    sid = str(existing.get('session_id') or '')
    if sid and repo and previous_repo != repo:
        previous_sessions = existing.setdefault('previous_sessions', [])
        if isinstance(previous_sessions, list):
            previous_sessions.append({
                'session_id': sid,
                'repo_path': previous_repo or '',
                'last_stage': existing.get('last_stage', ''),
                'saved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            existing['previous_sessions'] = previous_sessions[-12:]
        sid = ''
        existing['session_reset_reason'] = 'repo_path_changed_or_missing; stale Claude context is not resumed across research bases'
    payload = {
        **existing,
        'project': project,
        'session_key': safe_session_key(session_key),
        'session_id': sid,
        'workspace_root': str(paths.root),
        'repo_path': repo,
        'status': existing.get('status') or 'ready',
        'created_at': existing.get('created_at') or dt.datetime.now(dt.timezone.utc).isoformat(),
        'updated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'resume_command': (f"cd {shlex.quote(str(paths.root))} && claude --resume {sid} --add-dir {shlex.quote(repo)}" if sid and repo else f"cd {shlex.quote(str(paths.root))} && claude --resume {sid}" if sid else 'Session will be created on first successful Claude Code call.'),
        'policy': 'Stage-scoped Claude Code session. Main experiment continuity is separate from writing, venue-intelligence, and preview-repair workers to avoid context cross-contamination.',
    }
    save_json(session_path(paths, session_key), payload)
    history_path(paths, session_key).parent.mkdir(parents=True, exist_ok=True)
    if not history_path(paths, session_key).exists():
        history_path(paths, session_key).write_text(f"# Claude Project Session\n\n- project: {project}\n- session_key: {safe_session_key(session_key)}\n- session_id: {sid}\n- workspace_root: {paths.root}\n- repo_path: {repo or 'none'}\n\n", encoding='utf-8')
    return payload



def build_context(project: str, instruction: str, stage: str, repo_path: str = '', agent_id: str = 'main') -> str:
    paths = build_paths(project)
    cfg = load_project_config(project)
    active = load_json(paths.state / 'active_repo.json', {})
    repo = resolve_session_repo_path(paths, stage, repo_path)
    current_env_route = current_environment_selection(paths)
    strategy = load_json(paths.state / 'repo_env_strategy.json', {})
    if not strategy and isinstance(active, dict) and isinstance(active.get('claude_repo_env_strategy'), dict):
        strategy = active.get('claude_repo_env_strategy', {})
    topic = cfg.get('topic', '') if isinstance(cfg, dict) else ''
    guidance = consume_guidance(project, target_agent_id='main' if agent_id == 'main' else agent_id, stage=stage)
    guidance_text = ''
    if guidance:
        guidance_text = '\n'.join(f"- {item.get('message', '')}" for item in guidance if item.get('message'))
    skill_files = local_skill_files()
    skills_text = skill_contract_summary() or 'none detected'
    trajectory_payload = load_json(paths.state / 'research_trajectory_system.json', {})
    direction_memory = load_json(paths.state / 'research_direction_memory.json', {})
    evidence_integrity = load_json(paths.state / 'research_evidence_integrity.json', {})
    optimization_plan = load_json(paths.state / 'trajectory_optimization_plan.json', {})
    trajectory_checkpoints = load_json(paths.state / 'trajectory_checkpoints.json', {})
    evolutionary_index = load_json(paths.state / 'evolutionary_memory_index.json', {})
    capability_audit = load_json(paths.state / 'research_trajectory_capability_audit.json', {})
    graph_history = load_json(paths.state / 'research_graph_history.json', {})
    landscape_assessment = load_json(paths.state / 'research_landscape_assessment.json', {})
    evidence_manifest = load_json(paths.state / 'research_evidence_manifest.json', {})
    memory_ledger = load_json(paths.state / 'evolutionary_memory_ledger.json', {})
    evo_cycle = load_recoverable_cycle(paths)
    third_party_stack = load_json(paths.state / 'third_party_research_stack.json', {})
    literature_packet = load_json(paths.state / 'literature_tool_packet.json', {})
    selected_execution_contract = current_find_execution_contract(paths)
    selected_execution_context = {
        'run_id': selected_execution_contract.get('run_id', ''),
        'selected_plan_id': selected_execution_contract.get('selected_plan_id', ''),
        'selected_idea_id': selected_execution_contract.get('selected_idea_id', ''),
        'selected_plan': selected_execution_contract.get('selected_plan') if isinstance(selected_execution_contract.get('selected_plan'), dict) else {},
        'selected_idea': selected_execution_contract.get('selected_idea') if isinstance(selected_execution_contract.get('selected_idea'), dict) else {},
        'selected_by': selected_execution_contract.get('selected_by', ''),
        'status': selected_execution_contract.get('status', ''),
        'reason': selected_execution_contract.get('reason', ''),
        'candidate_counts': selected_execution_contract.get('candidate_counts') if isinstance(selected_execution_contract.get('candidate_counts'), dict) else {},
        'execution_policy': selected_execution_contract.get('execution_policy') if isinstance(selected_execution_contract.get('execution_policy'), dict) else {},
    }
    literature_last_run = load_json(paths.state / 'literature_tool_last_run.json', {})
    find_progress = load_json(paths.planning / 'finding' / 'find_progress.json', {})
    submission_readiness = load_json(paths.state / 'submission_readiness.json', {})
    packet_summary = literature_packet.get('summary', {}) if isinstance(literature_packet, dict) and isinstance(literature_packet.get('summary'), dict) else {}
    current_find_context = _current_find_stage(stage)
    authoritative_literature_gate = {
        'run_id': find_progress.get('run_id') if isinstance(find_progress, dict) else literature_packet.get('run_id') if isinstance(literature_packet, dict) else '',
        'strong_recommendations': find_progress.get('strong_recommendation_count') if isinstance(find_progress, dict) else packet_summary.get('strong_paper_anchors'),
        'recommendation_target_count': find_progress.get('recommendation_target_count') if isinstance(find_progress, dict) else packet_summary.get('recommendation_target_count'),
        'recommendation_shortfall': find_progress.get('recommendation_shortfall') if isinstance(find_progress, dict) else packet_summary.get('recommendation_shortfall'),
        'source': 'planning/finding/find_progress.json overrides stale literature_tool_packet status',
    }
    native_capability_bindings = []
    if isinstance(third_party_stack, dict):
        native_capability_bindings = [
            {
                'capability': row.get('capability', ''),
                'native_contract': row.get('native_contract') or row.get('contract') or row.get('uses', []),
            }
            for row in third_party_stack.get('capability_bindings', [])
            if isinstance(row, dict)
        ][:8]
    key_files = [
        paths.config,
        paths.state / 'active_repo.json',
        paths.state / 'repo_env_strategy.json',
        paths.state / 'repo_data_requirements.json',
        paths.state / 'real_dataset_probe.json',
        paths.state / 'data_unavailability_policy.json',
        paths.state / 'evidence_ready_repo_selection.json',
        paths.state / 'research_trajectory_system.json',
        paths.state / 'research_memory.json',
        paths.state / 'research_direction_memory.json',
        paths.state / 'research_evidence_integrity.json',
        paths.state / 'trajectory_optimization_plan.json',
        paths.state / 'trajectory_checkpoints.json',
        paths.state / 'evolutionary_memory_index.json',
        paths.state / 'research_trajectory_capability_audit.json',
        paths.state / 'research_graph_history.json',
        paths.state / 'research_landscape_assessment.json',
        paths.state / 'research_evidence_manifest.json',
        paths.state / 'evolutionary_memory_ledger.json',
        paths.state / 'research_landscape.json',
        paths.state / 'novelty_map.json',
        paths.state / 'failed_hypothesis_graph.json',
        paths.state / 'unexplored_niche_graph.json',
        paths.state / 'research_assurance_layer.json',
        paths.state / 'recoverable_cycle_summary.json',
        paths.state / 'evidence_review_board.json',
        paths.state / 'evo_recoverable_memory.json',
        paths.state / 'research_skill_contracts.json',
        paths.state / 'third_party_research_stack.json',
        paths.state / 'current_find_research_plan.json',
        paths.state / 'experiment_plan.json',
        paths.state / 'taste_plan_bridge.json',
        paths.state / 'idea_candidates.json',
        paths.state / 'fresh_base_implementation_plan.json',
        paths.state / 'literature_tool_packet.json',
        paths.state / 'literature_tool_last_run.json',
        paths.state / 'taste_literature_intermediates.json',
        paths.state / 'taste_sync.json',
        paths.state / 'finding_frontend.json',
        paths.planning / 'reference_workflow_and_claude_code.md',
        paths.planning / 'literature_tool_packet.md',
        paths.planning / 'finding' / 'find_results.json',
        paths.planning / 'finding' / 'article.md',
        paths.planning / 'finding' / 'read.md',
        paths.planning / 'finding' / 'idea.md',
        paths.planning / 'finding' / 'plan.md',
        paths.planning / 'finding' / 'category_scan_report.json',
        paths.planning / 'finding' / 'title_filter_report.json',
        paths.planning / 'finding' / 'arxiv_raw.json',
        paths.planning / 'finding' / 'arxiv_prefiltered.json',
        paths.reports / 'evidence_ready_repo_claude_review.md',
        paths.reports / 'status.md',
        Path(repo) / 'README.md' if repo else Path(''),
        Path(repo) / 'main.py' if repo else Path(''),
        Path(repo) / 'utils' / 'dataloader.py' if repo else Path(''),
        *skill_files,
    ]
    existing = [str(path) for path in key_files if str(path) and path.exists()]
    if current_find_context:
        literature_context = {
            'status': literature_packet.get('status') if isinstance(literature_packet, dict) else '',
            'summary': literature_packet.get('summary', {}) if isinstance(literature_packet, dict) else {},
            'coverage': literature_packet.get('coverage', {}) if isinstance(literature_packet, dict) else {},
            'suggested_followup_queries': literature_packet.get('suggested_followup_queries', [])[:8] if isinstance(literature_packet, dict) and isinstance(literature_packet.get('suggested_followup_queries', []), list) else [],
            'last_run': {
                'status': literature_last_run.get('status'),
                'current_find_run_id': literature_last_run.get('current_find_run_id'),
                'current_strong_recommendations': literature_last_run.get('current_strong_recommendations'),
                'current_recommendation_target_count': literature_last_run.get('current_recommendation_target_count'),
                'current_recommendation_shortfall': literature_last_run.get('current_recommendation_shortfall'),
            } if isinstance(literature_last_run, dict) else {},
            'packet_json_path': str(paths.state / 'literature_tool_packet.json'),
            'packet_markdown_path': str(paths.planning / 'literature_tool_packet.md'),
            'note': 'Current-Find Read/Idea/Plan must read planning/finding/find_results.json and full_text_reading/full_text_packet.json directly; large paper rows are intentionally not inlined in the Claude startup prompt.',
        } if isinstance(literature_packet, dict) and literature_packet else 'not built yet; read planning/finding/find_results.json and run the TASTE literature wrapper only if the current packet is missing or stale'
    else:
        literature_context = {
            'status': literature_packet.get('status'),
            'summary': literature_packet.get('summary', {}),
            'coverage': literature_packet.get('coverage', {}),
            'strong_papers': literature_packet.get('strong_papers', [])[:8],
            'base_work_candidates': literature_packet.get('base_work_candidates', [])[:6],
            'suggested_followup_queries': literature_packet.get('suggested_followup_queries', [])[:10],
            'last_run': literature_last_run,
        } if isinstance(literature_packet, dict) and literature_packet else 'not built yet; run scripts/build_literature_tool_packet.py --project ' + project + ' before literature-dependent decisions'
    literature_context_text = json.dumps(literature_context, ensure_ascii=False, indent=2) if isinstance(literature_context, dict) else str(literature_context)
    return f"""
You are the persistent Claude Code session for research project `{project}`.

User/TASTE instruction:
{instruction}

Queued user guidance from the web UI:
{guidance_text or 'none'}

Stage: {stage}
Topic: {topic}
research project root: {paths.root}
Selected repo: {repo or 'none'}
Current environment-stage route selection:
{json.dumps(current_env_route, ensure_ascii=False, indent=2)}

Claude repo/data/env stewardship memory:
{json.dumps(strategy, ensure_ascii=False, indent=2) if isinstance(strategy, dict) and strategy else 'none yet'}

Research trajectory system memory:
{json.dumps(trajectory_payload.get('summary', {}), ensure_ascii=False, indent=2) if isinstance(trajectory_payload, dict) else 'none yet'}

Long-term direction/evidence/optimization memory:
{json.dumps({'direction_entries': len(direction_memory.get('history', [])) if isinstance(direction_memory, dict) else 0, 'latest_direction': direction_memory.get('latest', {}) if isinstance(direction_memory, dict) else {}, 'evidence_integrity': {'status': evidence_integrity.get('status'), 'issue_count': len(evidence_integrity.get('issues', [])) if isinstance(evidence_integrity, dict) and isinstance(evidence_integrity.get('issues', []), list) else 0}, 'optimization_queue_size': optimization_plan.get('queue_size', 0) if isinstance(optimization_plan, dict) else 0, 'next_queue_items': optimization_plan.get('queue', [])[:5] if isinstance(optimization_plan, dict) and isinstance(optimization_plan.get('queue', []), list) else [], 'trajectory_checkpoint': trajectory_checkpoints.get('latest', {}) if isinstance(trajectory_checkpoints, dict) else {}, 'evolutionary_index': {'indexed_item_count': evolutionary_index.get('indexed_item_count', 0), 'inheritance_rules': evolutionary_index.get('inheritance_rules', [])[:4]} if isinstance(evolutionary_index, dict) else {}, 'capability_audit': {'overall_status': capability_audit.get('overall_status'), 'capability_status': capability_audit.get('capability_status'), 'module_statuses': {row.get('module'): row.get('status') for row in capability_audit.get('modules', []) if isinstance(row, dict)}} if isinstance(capability_audit, dict) else {}, 'graph_history': {'history_count': graph_history.get('history_count', 0), 'latest_hash': (graph_history.get('latest', {}) if isinstance(graph_history.get('latest', {}), dict) else {}).get('snapshot_hash', '')} if isinstance(graph_history, dict) else {}, 'landscape_assessment': {'status': landscape_assessment.get('status'), 'risk_notes': landscape_assessment.get('risk_notes', [])[:4]} if isinstance(landscape_assessment, dict) else {}, 'evidence_manifest': {'ref_count': evidence_manifest.get('ref_count', 0), 'weak_or_unsupported_claims': evidence_manifest.get('weak_or_unsupported_claims', [])[:6]} if isinstance(evidence_manifest, dict) else {}, 'evolutionary_memory_ledger': {'history_count': memory_ledger.get('history_count', 0), 'latest_counts': (memory_ledger.get('latest', {}) if isinstance(memory_ledger.get('latest', {}), dict) else {}).get('counts', {})} if isinstance(memory_ledger, dict) else {}}, ensure_ascii=False, indent=2)}

TASTE recoverable trajectory-cycle memory:
{json.dumps({'status': evo_cycle.get('status') or evo_cycle.get('final_status'), 'phase_count': evo_cycle.get('phase_count') or len(evo_cycle.get('phases', [])) if isinstance(evo_cycle, dict) else 0, 'recoverable_exception_count': evo_cycle.get('recoverable_exception_count', 0) if isinstance(evo_cycle, dict) else 0}, ensure_ascii=False, indent=2) if isinstance(evo_cycle, dict) else 'none yet'}

native method capability contracts:
{json.dumps({'status': third_party_stack.get('status'), 'summary': third_party_stack.get('summary', {}), 'capability_bindings': native_capability_bindings}, ensure_ascii=False, indent=2) if isinstance(third_party_stack, dict) and third_party_stack else 'not yet synced; run scripts/sync_third_party_research_stack.py before relying on native method contracts'}

Method provenance is retained for audit only in `state/third_party_research_stack.json`. Do not mention source-project names in operational plans, role names, progress summaries, or paper prose; use native module names instead.

Local TASTE Claude skills that you must treat as executable contracts when relevant:
{skills_text}

TASTE authoritative literature/submission gates:
{json.dumps({'literature_gate': authoritative_literature_gate, 'submission_readiness': {'status': submission_readiness.get('status') if isinstance(submission_readiness, dict) else '', 'submission_ready': submission_readiness.get('submission_ready') if isinstance(submission_readiness, dict) else False, 'failed_checks': submission_readiness.get('failed_checks', [])[:8] if isinstance(submission_readiness, dict) and isinstance(submission_readiness.get('failed_checks', []), list) else []}}, ensure_ascii=False, indent=2)}

TASTE literature tool packet:
{literature_context_text}

Current-Find selected execution contract:
{json.dumps(selected_execution_context, ensure_ascii=False, indent=2)}

TASTE reference workflow and Claude Code routing policy:
Read `planning/reference_workflow_and_claude_code.md` before route/base/experiment/paper decisions. Follow its stage contract and tool routing unless local evidence proves a narrower action is required.

You must autonomously inspect local files and code. Do not treat any human/assistant prior analysis as evidence.
Use these files as starting points, and read additional files if needed:
{chr(10).join('- ' + item for item in existing)}

Hard rules:
- Work only inside the research project root and selected repo listed above.
- `state/evidence_ready_repo_selection.json` with `selection_stage=environment_claude_code` is authoritative for the current route; stale `active_repo.json` or legacy/control routes cannot override it.
- If a wrapper-managed full reference reproduction audit has passed for the current selected base, do not overwrite `state/evidence_ready_repo_selection.json` or `state/active_repo.json` with a legacy/control route. If local evidence suggests a switch may be needed, write a non-authoritative route-switch rationale/proposal and keep gates blocked until TASTE deterministic base-switch gates authorize the change.
- Failed, weak, or negative experiments are internal audit/prune evidence only. Do not turn them into the submission story, do not recommend writing them as paper contributions, and do not automatically re-scope the user topic to a weaker subset just to make a paper. If the current route cannot support the target topic, record a blocked state plus a route proposal for deterministic TASTE gates.
- `planning/finding/find_progress.json` is authoritative for strong-recommendation target and shortfall. If it says shortfall > 0, do not edit JSON to mark literature exhausted/submission_ready; run targeted survey/scoring repair instead.
- Do not fabricate metrics, claims, citations, data availability, or paper readiness.
- Cite exact local files/paths you inspected.
- Do not recreate or mutate a locked conda environment unless explicitly instructed and justified by local evidence.
- If stewardship memory exists, follow it unless your fresh local inspection contradicts it; if contradicted, explain the evidence and write the needed repo/env/data action for TASTE.
- You own repo, data, and conda-environment implementation decisions for this project: decide whether to keep/modify the current repo or switch/search, whether to reuse/repair/create a project env, and whether to use/download/place/search data.
- Never silently delete an existing conda environment; if a rebuild is needed, create or recommend a new project-specific env and preserve old state.
- Use only loader-ready real datasets for experiment/paper evidence; synthetic smoke is not paper evidence.
- Use the research trajectory files as persistent memory: update or respect research_landscape, novelty_map, failed_hypothesis_graph, unexplored_niche_graph, research_memory, research_direction_memory, research_graph_history, research_landscape_assessment, research_evidence_integrity, research_evidence_manifest, trajectory_optimization_plan, trajectory_checkpoints, evolutionary_memory_index, evolutionary_memory_ledger, research_trajectory_capability_audit, and research_assurance_layer before changing direction.
- If research_trajectory_capability_audit reports a blocked capability, repair the capability infrastructure before treating any experiment/paper output as reliable.
- For experiment-loop, evidence-gate, and paper-writing work, explicitly follow the local .claude/skills contracts listed above.
- Follow `planning/reference_workflow_and_claude_code.md`: choose the correct route/tool for the current stage; do not run experiments or paper repair from fallback literature artifacts.
- If a training process is already alive, observe it non-invasively only: do not send signals, attach tracing/debuggers, read blocking `/proc/<pid>/fd/*` pipes, kill, restart, or launch a duplicate unless the process has exited or artifact-local evidence proves a hard failure.
- New training launches must go through the the launcher with two explicit interpreters: `{management_python()} scripts/launch_experiment_run.py --project {project} --artifact-name <unique_slug> --cwd <project_or_repo_dir> -- <project-experiment-python> -u <training_script.py> ...`.
- Do not use system `python`, bare `python3`, `conda run`, raw `nohup`, shell backgrounding, or manual stdout redirection for new experiments. The launcher will reject them.
- The launcher creates `run_contract.json`, `run.lock`, `launcher.pid.json`, and `stdout_stderr.log`, rejects reused/contaminated artifact dirs, records `python_executable`, `environment_contract`, `expected_outputs`, and gives TASTE one PID/log/artifact contract to monitor.
- If a repo training script cannot accept an artifact path or unbuffered logging, write a small repo-local wrapper once, then launch that wrapper through `scripts/launch_experiment_run.py`; do not create ad-hoc background shell jobs.
- An empty log while a process is alive is not evidence of failure. Record that the run is still waiting for output and keep monitoring non-invasively.
- Before stopping a run, write the stop reason, PID, command, artifact path, and evidence to an artifact-local audit or run note.
- Use TASTE's native research-direction, evolutionary-memory, evidence-assurance, trajectory-optimization, and paper-production contracts. If the method stack is absent or stale, run `scripts/sync_third_party_research_stack.py --project {project}` first.
- Preserve method provenance in audit state when required, but do not surface external source-project names as active agents or roles.
- Read TASTE recoverable-cycle memory files before deciding whether to retry, repair, prune, or switch direction.
- Before selecting a base paper, generating an idea, modifying code from a paper, or writing literature-related claims, first read `state/current_find_research_plan.json`, `state/experiment_plan.json`, and `state/taste_plan_bridge.json`; then read `planning/literature_tool_packet.md` or `state/literature_tool_packet.json` and at least one raw artifact under `planning/finding/`.
- If the packet is missing, stale, too generic, or does not cover the current blocker, run `{management_python()} scripts/run_literature_tool.py --project {project} --query "<targeted research query>" --fast-mode` for a narrow refresh, or add `--deep-survey` when the route depends on broad conference/arXiv coverage. Then rerun `{management_python()} scripts/build_literature_tool_packet.py --project {project}`.
- Use the literature tool only as TASTE's internal survey capability. Do not describe it as a separate agent or outsource decisions to it.
- Reuse survey intermediate files (`find_results.json`, `read_results.json`, `ideas.json`, `plans.json`, category/title/arXiv reports) for idea generation, base-work switching, repo selection, and experiment planning instead of redoing blind search.
- Current-Find Read/Idea/Plan stage (`current-find-claude-read-idea-plan`) is responsible for reading every recommended paper through auditable Task/subagent delegation, generating exactly 5 three-part ideas, generating exactly 5 plans, and choosing exactly one best plan by writing one non-empty `selected_plan_id` with `selected_for_execution=true` and `execute_next=true`; the other plans are backlog only.
- Downstream stages after Current-Find may consume only `selected_plan_id` from the selected execution contract above. Non-selected ideas/plans are supervision backlog only and must not drive environment, experiment, writing, or claim work.
- In downstream stages, if current-Find candidates exist and `selected_plan_id` is empty or ambiguous, stop downstream work and ask the wrapper/current-Find Claude selection stage to rebuild `state/current_find_research_plan.json`, `state/taste_plan_bridge.json`, and `state/experiment_plan.json`; do not choose an execution route ad hoc.
- Treat literature signals as planning evidence only. Paper claims still require local repo/data/env audits, experiment logs, metrics, bad-case/counterexample artifacts, and citation metadata.
- Do not treat `active_repo.json` as current unless `evidence_ready_repo_selection.json` has selection_stage=environment_claude_code and fresh_find_run_id equals the current Find run. Current-Find Read/Idea/Plan stages must not bind to any repo.
- Optimize the whole trajectory, not a single response: propose repair/search/experiment actions that preserve evidence, memory, and stop conditions.
- Keep git hygiene: never add research-object repos, datasets, generated PDFs, checkpoints, logs, or runtime state to git.
- If a selected base lacks one of the configured topic components, remember it is only evidence-ready for the covered components until you independently add and validate the missing components.
- Obsolete baseline/route cleanup is a project-context decision. Inspect `state/obsolete_baseline_cleanup_plan.json` when it is blocked. If candidate files must be kept, write `state/obsolete_baseline_cleanup_review.json` with `cleanup_authorized=false`, `current_route_reviewed=true`, `protected_current_route=true`, `reviewed_candidate_count`, `candidate_fingerprint` copied from the plan, and rationale. If cleanup is required, first write `state/obsolete_baseline_cleanup_authorization.json` with exact approved paths and protected paths. When TASTE starts a cleanup-execution stage, execute only those approved exact paths yourself, prefer reversible project-local archival unless evidence requires deletion, and write `state/obsolete_baseline_cleanup_execution.json` with `status=completed_by_project_claude`, `cleanup_executed=true`, exact `applied_paths`, exact `remaining_candidate_paths`, protected paths, and rationale. Outside that cleanup-execution stage, do not delete/archive project files.

For autonomous experiment iteration, keep calling tools and repairing until the local problem is actually resolved or you can prove it is blocked by missing data/account/compute. Do not stop after a shallow inspection.

Return concise Markdown with: Conclusion, Evidence Inspected, Risks/Gaps, Actions Taken, Next Actions.
""".strip()

def run_claude(project: str, instruction: str, stage: str, timeout_sec: int, resume: bool = True, agent_id: str = 'main', repo_path: str = '') -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    coding_cfg = cfg.get('coding_agent', {}) if isinstance(cfg.get('coding_agent', {}), dict) else {}
    requested_repo = resolve_session_repo_path(paths, stage, repo_path)
    session_key = session_key_for(agent_id, stage)
    session = ensure_session(project, requested_repo, session_key=session_key)
    claude = find_claude(cfg)
    repo = session.get('repo_path') or requested_repo
    prompt = build_context(project, instruction, stage, str(repo or ''), agent_id=agent_id)
    prompt_file = keyed_state_path(paths, 'claude_project_session_last_prompt', session_key, suffix='.txt')
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt, encoding='utf-8')
    permission_mode = str(coding_cfg.get('claude_permission_mode', 'bypassPermissions'))
    output_format = str(coding_cfg.get('claude_output_format', 'stream-json') or 'stream-json')
    cmd = [claude, '-p', '--permission-mode', permission_mode, '--output-format', output_format]
    if output_format == 'stream-json':
        cmd.extend(['--verbose', '--include-partial-messages'])
    cmd.extend(['--add-dir', str(paths.root)])
    if repo:
        cmd.extend(['--add-dir', str(repo)])
    reset_reason = fresh_session_reason(stage, paths, session_key=session_key)
    if reset_reason:
        resume = False
    session_id = str(session.get('session_id') or '') if resume else ''
    if resume and session_id:
        cmd.extend(['--resume', session_id])
    model = str(coding_cfg.get('claude_model') or '').strip()
    if model:
        cmd.extend(['--model', model])
    launch_command = ' '.join(shlex.quote(item) for item in cmd) + ' < ' + shlex.quote(str(prompt_file))
    env = interactive_env(project, cfg)
    # TASTE sessions are non-interactive wrapper calls; plugin marketplace auto-install/update
    # can block startup on network git clones before Claude emits any stream events.
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
    env.pop("FORCE_AUTOUPDATE_PLUGINS", None)
    if env.get("USE_EXISTING_LITERATURE_PACKET") and stage in {"current-find-claude-read-idea-plan"}:
        env.pop("DISABLE_NEW_FIND", None)
    elif stage in {"current-find-claude-read-idea-plan"}:
        env.pop("DISABLE_NEW_FIND", None)
    if Path(claude).exists():
        env['PATH'] = os.pathsep.join([str(Path(claude).parent), env.get('PATH', '')])
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    effective_timeout = max(0, int(timeout_sec or 0))
    timeout_label = 'disabled' if effective_timeout <= 0 else f'{max(30, effective_timeout)}s'
    try:
        first_output_timeout = int(env.get('CLAUDE_FIRST_OUTPUT_TIMEOUT_SEC') or coding_cfg.get('claude_first_output_timeout_sec') or 180)
    except Exception:
        first_output_timeout = 180
    first_output_timeout = max(30, first_output_timeout)
    no_event_timeout = claude_no_event_timeout_seconds(stage, effective_timeout, env, coding_cfg)
    try:
        max_partial_output_bytes = int(env.get('CLAUDE_MAX_PARTIAL_OUTPUT_BYTES') or coding_cfg.get('claude_max_partial_output_bytes') or 8_000_000)
    except Exception:
        max_partial_output_bytes = 8_000_000
    max_partial_output_bytes = max(1_000_000, max_partial_output_bytes)
    try:
        max_stdout_chunks_per_tick = int(env.get('CLAUDE_MAX_STDOUT_CHUNKS_PER_TICK') or coding_cfg.get('claude_max_stdout_chunks_per_tick') or 64)
    except Exception:
        max_stdout_chunks_per_tick = 64
    max_stdout_chunks_per_tick = max(1, min(max_stdout_chunks_per_tick, 512))
    upsert_agent(
        project,
        agent_id,
        name='Claude Code',
        role='claude-main' if agent_id == 'main' else 'claude-worker',
        stage=stage,
        status='running',
        goal=instruction[:500],
        parent_id='main' if agent_id != 'main' else '',
        command=cmd,
        current_step='starting Claude Code',
        extra={'workspace_root': str(paths.root), 'repo_path': repo, 'timeout_sec': effective_timeout, 'fresh_session_reason': reset_reason},
    )

    def emit(message: str) -> None:
        print(message, flush=True)
        append_agent_log(project, agent_id, message)

    emit(f"claude: starting persistent project session for {project}")
    emit(f"claude: session_key={session_key}")
    emit(f"claude: workspace={paths.root}")
    emit(f"claude: repo={repo or 'none'}")
    emit(f"claude: executable={claude}")
    emit(f"claude: permission_mode={permission_mode} output_format={output_format} timeout={timeout_label} first_output_timeout={first_output_timeout}s no_event_timeout={no_event_timeout}s max_stdout_chunks_per_tick={max_stdout_chunks_per_tick}")
    if reset_reason:
        emit(f"claude: starting fresh session context ({reset_reason}); previous session is retained only for audit.")

    raw_lines: list[str] = []
    human_lines: list[str] = []
    json_events: list[dict[str, Any]] = []
    partial_text: list[str] = []
    streamed_text: list[str] = []
    announced_session_ids: set[str] = set()
    announced_tool_ids: set[str] = set()
    tool_blocks: dict[str, dict[str, Any]] = {}
    tool_index_to_id: dict[str, str] = {}
    parsed: dict[str, Any] = {}
    return_code = 1
    timed_out = False
    route_guard_tripped = False
    route_guard_report: dict[str, Any] = {}
    tool_policy_tripped = False
    tool_policy_report: dict[str, Any] = {}
    last_route_guard_check = 0.0
    startup_silent_timeout = False
    no_event_stream_timeout = False
    partial_output_overflow = False

    def remember_message(text: str) -> None:
        text = redact_secrets(text).rstrip()
        if not text:
            return
        human_lines.append(text)
        emit(text)

    def compact(value: Any, limit: int = 180) -> str:
        if value is None:
            return ''
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value)
        text = redact_secrets(text)
        text = ' '.join(text.replace('\n', ' ').split())
        return text[:limit] + ('...' if len(text) > limit else '')

    def bash_tool_policy_issue(command: str) -> str:
        return bash_command_tool_policy_issue(command, project, stage)

    def inspect_tool_policy(name: str, tool_input: Any) -> None:
        nonlocal tool_policy_tripped, tool_policy_report
        if tool_policy_tripped:
            return
        data = tool_input if isinstance(tool_input, dict) else {}
        label = str(name or '')
        command = str(data.get('command') or data.get('cmd') or data.get('script') or tool_input or '')
        reason = bash_tool_policy_issue(command) if label == 'Bash' else current_find_tool_policy_issue(label, tool_input, stage)
        if not reason:
            return
        if is_current_find_gate_state_policy_reason(reason):
            policy_type = 'current_find_gate_state_writer'
            policy_text = (
                'Current-Find gate/state is owned by the wrapper. Claude may write only the '
                'planning/finding Read/Idea/Plan content artifacts; TASTE writes state files after '
                'machine validation so failed execution cannot appear ready.'
            )
        elif is_current_find_artifact_policy_reason(reason):
            policy_type = 'current_find_artifact_writer'
            policy_text = (
                'Current-Find Read/Idea/Plan artifact writing is recoverable but must be authored through '
                'Claude file tools after reading full-text evidence. Bash/Python generators and JSON Edit/MultiEdit on '
                'read_results.json, ideas.json, or plans.json are blocked because they can fabricate, bulk-patch, or partially corrupt scientific content; '
                'single-paper deep-read fragment JSON files may be repaired with Claude file tools.'
            )
        elif 'trajectory supervisor recursion' in reason:
            policy_type = 'trajectory_supervisor_recursion'
            policy_text = 'Trajectory workers must not spawn nested trajectory supervisors.'
        else:
            policy_type = 'experiment_launcher'
            policy_text = 'Claude Code may not bypass TASTE control wrappers. New experiment launches must use scripts/launch_experiment_run.py with the project experiment Python after `--`.'
        tool_policy_tripped = True
        tool_policy_report = {
            'status': 'blocked',
            'reason': reason,
            'policy_type': policy_type,
            'recoverable_by_current_find_repair': policy_type in {'current_find_artifact_writer', 'current_find_gate_state_writer'},
            'terminate_current_turn': True,
            'termination_reason': 'current_find_repair_required' if policy_type in {'current_find_artifact_writer', 'current_find_gate_state_writer'} else 'tool_policy_violation',
            'tool': label,
            'command': redact_secrets(command),
            'stage': stage,
            'policy': policy_text,
            'blocked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        save_json(paths.state / 'claude_tool_policy_last_block.json', tool_policy_report)
        active_proc = proc
        if active_proc is not None and active_proc.poll() is None:
            tool_policy_report['process_terminate_requested_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_json(paths.state / 'claude_tool_policy_last_block.json', tool_policy_report)
            stop_process(active_proc, signal.SIGTERM)
            try:
                active_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tool_policy_report['process_sigkill_requested_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
                save_json(paths.state / 'claude_tool_policy_last_block.json', tool_policy_report)
                stop_process(active_proc, signal.SIGKILL)
        if 'trajectory supervisor recursion' in reason:
            recursion_report = dict(tool_policy_report)
            recursion_report.update({
                'status': 'blocked_recursion_guard',
                'reason': 'claude_bash_invoked_trajectory_supervisor',
                'agent_id': agent_id,
                'blocked_at': tool_policy_report['blocked_at'],
            })
            save_json(paths.state / 'trajectory_supervisor_recursion_guard.json', recursion_report)
            remember_message('claude: trajectory supervisor recursion guard blocked a nested supervisor launch; complete the assigned queue item instead.')
        elif is_current_find_gate_state_policy_reason(reason):
            remember_message('claude: current-Find gate/state writer policy blocked a direct state-file edit; wrapper will write those state files after machine validation.')
        elif is_current_find_artifact_policy_reason(reason):
            remember_message('claude: current-Find artifact writer policy blocked unsafe artifact writing; The workflow will restart this takeover with a repair prompt. Claude must use Claude file tools for per-paper deep-read fragments and complete Write calls for ideas/plans after reading the full-text files.')
        else:
            remember_message('claude: Bash tool policy blocked a naked experiment launch; terminating this Claude turn so TASTE can restart with launcher-managed experiment control.')

    def summarize_tool(name: str, tool_input: Any) -> str:
        data = tool_input if isinstance(tool_input, dict) else {}
        label = name or 'unknown'
        inspect_tool_policy(label, tool_input)
        if label == 'Bash':
            command = compact(data.get('command') or data.get('cmd') or data.get('script') or tool_input, 260)
            return f"Bash command={command or '[input unavailable in stream]'}"
        if label in {'Read', 'Edit', 'MultiEdit', 'Write'}:
            file_path = compact(data.get('file_path') or data.get('file') or data.get('path') or data.get('filename') or tool_input, 220)
            return f"{label} file={file_path or '[input unavailable in stream]'}"
        if label == 'Grep':
            return f"Grep pattern={compact(data.get('pattern'), 120)} path={compact(data.get('path'), 160)}"
        if label == 'Glob':
            return f"Glob pattern={compact(data.get('pattern'), 160)} path={compact(data.get('path'), 160)}"
        if label == 'LS':
            return f"LS path={compact(data.get('path'), 220)}"
        if label == 'TodoWrite':
            todos = data.get('todos', [])
            count = len(todos) if isinstance(todos, list) else 0
            return f"TodoWrite todos={count}"
        if label == 'WebFetch':
            return f"WebFetch url={compact(data.get('url'), 220)} prompt={compact(data.get('prompt'), 120)}"
        return f"{label} input={compact(tool_input, 240)}" if tool_input else label

    def parse_tool_input(raw: Any) -> Any:
        if isinstance(raw, dict) and raw:
            return raw
        text = ''.join(raw) if isinstance(raw, list) else str(raw or '')
        if not text.strip():
            return raw
        try:
            return json.loads(text)
        except Exception:
            return text

    def normalized(text: str) -> str:
        return ' '.join(str(text or '').split())

    def flush_partial(force: bool = False) -> None:
        text = ''.join(partial_text).strip()
        if not text:
            partial_text.clear()
            return
        if force or len(text) >= 100 or text.endswith(('。', '！', '？', '.', '!', '?', '\n')):
            streamed_text.append(text)
            remember_message(f"Claude: {text}")
            partial_text.clear()

    def summarize_event(event: dict[str, Any]) -> str:
        event_type = str(event.get('type') or event.get('subtype') or '').strip()
        if event_type == 'system':
            sid = event.get('session_id') or event.get('sessionId') or ''
            if sid and str(sid) in announced_session_ids:
                return ''
            if sid:
                announced_session_ids.add(str(sid))
            return f"claude: session initialized{f' ({sid})' if sid else ''}"
        if event_type == 'stream_event':
            inner = event.get('event') if isinstance(event.get('event'), dict) else {}
            inner_type = str(inner.get('type') or '').strip()
            if inner_type == 'content_block_start':
                content = inner.get('content_block') if isinstance(inner.get('content_block'), dict) else {}
                if content.get('type') == 'tool_use':
                    tool_id = str(content.get('id') or '')
                    index = str(inner.get('index') if inner.get('index') is not None else '')
                    if tool_id:
                        tool_index_to_id[index] = tool_id
                        tool_blocks[tool_id] = {
                            'name': str(content.get('name') or 'unknown'),
                            'input': content.get('input') if isinstance(content.get('input'), dict) else {},
                            'partial_json': '',
                        }
                    if tool_id and tool_id in announced_tool_ids:
                        return ''
                    tool_input = content.get('input') if isinstance(content.get('input'), dict) else {}
                    if not tool_input:
                        return ''
                    if tool_id:
                        announced_tool_ids.add(tool_id)
                    return f"Claude 调用工具: {summarize_tool(str(content.get('name') or 'unknown'), tool_input)}"
            if inner_type == 'content_block_delta':
                delta = inner.get('delta') if isinstance(inner.get('delta'), dict) else {}
                if delta.get('type') == 'input_json_delta':
                    index = str(inner.get('index') if inner.get('index') is not None else '')
                    tool_id = tool_index_to_id.get(index, '')
                    if tool_id:
                        block = tool_blocks.setdefault(tool_id, {'name': 'unknown', 'input': {}, 'partial_json': ''})
                        block['partial_json'] = str(block.get('partial_json') or '') + str(delta.get('partial_json') or '')
                    return ''
            if inner_type in {'content_block_stop', 'message_stop'}:
                if inner_type == 'content_block_stop':
                    index = str(inner.get('index') if inner.get('index') is not None else '')
                    tool_id = tool_index_to_id.get(index, '')
                    if tool_id and tool_id not in announced_tool_ids:
                        block = tool_blocks.get(tool_id, {})
                        tool_input = block.get('input') if isinstance(block.get('input'), dict) and block.get('input') else parse_tool_input(block.get('partial_json', ''))
                        announced_tool_ids.add(tool_id)
                        return f"Claude 调用工具: {summarize_tool(str(block.get('name') or 'unknown'), tool_input)}"
                flush_partial(force=True)
            return ''
        if event_type == 'assistant':
            flush_partial(force=True)
            message = event.get('message') if isinstance(event.get('message'), dict) else event
            content = message.get('content') if isinstance(message, dict) else None
            parts: list[str] = []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get('type') == 'text' and item.get('text'):
                        parts.append(str(item.get('text')))
                    elif item.get('type') == 'tool_use':
                        tool_id = str(item.get('id') or '')
                        if tool_id and tool_id in announced_tool_ids:
                            continue
                        if tool_id:
                            announced_tool_ids.add(tool_id)
                        parts.append(f"调用工具: {summarize_tool(str(item.get('name') or 'unknown'), item.get('input'))}")
            elif isinstance(content, str):
                parts.append(content)
            text = '\n'.join(part.strip() for part in parts if part and part.strip())
            if text and normalized(text) and normalized(text) in normalized('\n'.join(streamed_text)):
                return ''
            return f"Claude: {text}" if text else ''
        if event_type == 'result' or event.get('result') is not None:
            flush_partial(force=True)
            text = str(event.get('result') or '').strip()
            if text and normalized(text) and normalized(text) in normalized('\n'.join(streamed_text + human_lines)):
                return f"claude: result {event.get('subtype') or 'complete'}"
            return f"Claude final:\n{text}" if text else f"claude: result {event.get('subtype') or 'complete'}"
        if event.get('error'):
            return f"claude error: {event.get('error')}"
        return ''

    def handle_output_line(line: str) -> None:
        nonlocal parsed
        text = line.rstrip('\n')
        raw_lines.append(text)
        stripped = text.strip()
        if not stripped:
            return
        if stripped.startswith('{'):
            try:
                event = json.loads(stripped)
                if isinstance(event, dict):
                    json_events.append(event)
                    if event.get('type') == 'stream_event':
                        inner = event.get('event') if isinstance(event.get('event'), dict) else {}
                        delta = inner.get('delta') if isinstance(inner.get('delta'), dict) else {}
                        if inner.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta' and delta.get('text'):
                            partial_text.append(str(delta.get('text')))
                            flush_partial()
                            return
                    if event.get('type') == 'result' or event.get('result') is not None:
                        parsed = event
                    message = summarize_event(event)
                    if message:
                        remember_message(message)
                    return
            except Exception:
                pass
        remember_message(stripped)

    def stop_process(proc: subprocess.Popen[str], sig: int) -> None:
        try:
            if hasattr(os, 'killpg'):
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def check_selected_base_route_guard(*, force: bool = False) -> bool:
        nonlocal route_guard_tripped, route_guard_report, last_route_guard_check
        if route_guard_tripped:
            return True
        now = time.monotonic()
        if not force and now - last_route_guard_check < 5:
            return False
        last_route_guard_check = now
        try:
            report = guard_selected_base_route(project, source_stage=f'{stage}:live')
        except Exception as exc:
            report = {'status': 'error', 'source_stage': f'{stage}:live', 'error': str(exc), 'repaired': False}
        if isinstance(report, dict) and report.get('repaired'):
            route_guard_report = report
            route_guard_tripped = True
            remember_message('claude: selected-base route guard blocked a legacy/control route overwrite; terminating this Claude turn so TASTE can restart from trusted selected-base state.')
            return True
        return False

    proc: subprocess.Popen[str] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        with prompt_file.open('r', encoding='utf-8') as prompt_handle:
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                stdin=prompt_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None,
            )
        if proc.stdout is None:
            raise RuntimeError('Claude Code process did not expose stdout.')
        selector = selectors.DefaultSelector()
        upsert_agent(project, agent_id, pid=proc.pid, status='running', current_step='Claude Code process started')
        selector.register(proc.stdout, selectors.EVENT_READ)
        stdout_fd = proc.stdout.fileno()
        os.set_blocking(stdout_fd, False)
        deadline = time.monotonic() + max(30, effective_timeout) if effective_timeout > 0 else None
        first_output_deadline = time.monotonic() + first_output_timeout
        last_heartbeat = time.monotonic()
        last_complete_event = last_heartbeat
        first_output_seen = False
        output_buffer = ''
        decoder = codecs.getincrementaldecoder('utf-8')('replace')

        def handle_output_bytes(chunk: bytes, *, final: bool = False) -> bool:
            nonlocal first_output_seen, output_buffer, last_complete_event
            saw_output = bool(chunk)
            if chunk:
                first_output_seen = True
                output_buffer += decoder.decode(chunk, final=False)
            if final:
                output_buffer += decoder.decode(b'', final=True)
            emitted = False
            while '\n' in output_buffer:
                line, output_buffer = output_buffer.split('\n', 1)
                handle_output_line(line + '\n')
                last_complete_event = time.monotonic()
                emitted = True
            if final and output_buffer:
                handle_output_line(output_buffer)
                last_complete_event = time.monotonic()
                output_buffer = ''
                emitted = True
            return saw_output or emitted

        def drain_stdout_available(*, final: bool = False) -> bool:
            saw = False
            chunks = 0
            while chunks < max_stdout_chunks_per_tick:
                try:
                    chunk = os.read(stdout_fd, 65536)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if not chunk:
                    break
                chunks += 1
                saw = handle_output_bytes(chunk) or saw
            if final:
                saw = handle_output_bytes(b'', final=True) or saw
            return saw

        while True:
            if not first_output_seen and time.monotonic() > first_output_deadline:
                startup_silent_timeout = True
                remember_message(f"claude: no stream output within {first_output_timeout}s; terminating startup-silent Claude Code call")
                stop_process(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process(proc, signal.SIGKILL)
                break
            if first_output_seen and output_buffer and len(output_buffer.encode('utf-8', errors='replace')) > max_partial_output_bytes:
                partial_output_overflow = True
                remember_message(f"claude: stream partial output exceeded {max_partial_output_bytes} bytes without a newline; terminating malformed stream")
                stop_process(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process(proc, signal.SIGKILL)
                break
            if first_output_seen and time.monotonic() - last_complete_event > no_event_timeout:
                no_event_stream_timeout = True
                remember_message(f"claude: no complete stream event for {no_event_timeout}s; terminating stalled Claude Code stream")
                stop_process(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process(proc, signal.SIGKILL)
                break
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                remember_message(f"claude: timed out after {max(30, effective_timeout)}s; terminating persistent session call")
                stop_process(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process(proc, signal.SIGKILL)
                break
            if proc.poll() is not None:
                if drain_stdout_available(final=True):
                    last_heartbeat = time.monotonic()
                break
            if tool_policy_tripped:
                stop_process(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process(proc, signal.SIGKILL)
                break
            if check_selected_base_route_guard():
                stop_process(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process(proc, signal.SIGKILL)
                break
            events = selector.select(timeout=0.5)
            for _key, _mask in events:
                if drain_stdout_available():
                    last_heartbeat = time.monotonic()
                    if tool_policy_tripped:
                        stop_process(proc, signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            stop_process(proc, signal.SIGKILL)
                        break
                    if check_selected_base_route_guard():
                        stop_process(proc, signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            stop_process(proc, signal.SIGKILL)
                        break
            if route_guard_tripped or tool_policy_tripped:
                break
            if time.monotonic() - last_heartbeat > 60:
                remember_message("claude: still running; waiting for Claude Code output")
                upsert_agent(project, agent_id, status='running', current_step='waiting for Claude Code output')
                last_heartbeat = time.monotonic()
        return_code = 126 if partial_output_overflow else 125 if startup_silent_timeout or no_event_stream_timeout else 124 if timed_out else int(proc.wait())
        if route_guard_tripped:
            return_code = 2
        if tool_policy_tripped:
            return_code = 3
    except FileNotFoundError:
        return_code = 127
        remember_message(
            f"Claude Code executable not found: {claude}. "
            "Use the Runtime panel to auto-detect or set claude_path/codex_path explicitly."
        )
    except Exception as exc:
        return_code = 1
        remember_message(f"claude: failed to run project session: {exc}")
    finally:
        if selector is not None:
            selector.close()
        if proc is not None and proc.poll() is None:
            stop_process(proc, signal.SIGTERM)

    flush_partial(force=True)
    if not parsed:
        for event in reversed(json_events):
            if event.get('type') == 'result' or event.get('result') is not None:
                parsed = event
                break
    if not parsed:
        for line in reversed(raw_lines):
            stripped = line.strip()
            if not stripped.startswith('{'):
                continue
            try:
                event = json.loads(stripped)
                if isinstance(event, dict):
                    parsed = event
                    break
            except Exception:
                continue
    raw_stdout = '\n'.join(raw_lines)
    stderr = ''
    stdout = str(parsed.get('result') or '').strip()
    if not stdout:
        stdout = '\n'.join(human_lines).strip() or raw_stdout
    returned_session_id = str(parsed.get('session_id') or ('' if reset_reason else session.get('session_id') or ''))
    if not returned_session_id:
        for event in reversed(json_events):
            candidate = event.get('session_id') or event.get('sessionId')
            if candidate:
                returned_session_id = str(candidate)
                break
    status = 'blocked_tool_policy' if tool_policy_tripped else 'blocked_selected_base_route_guard' if route_guard_tripped else 'partial_output_overflow' if partial_output_overflow else 'startup_silent_timeout' if startup_silent_timeout else 'no_event_stream_timeout' if no_event_stream_timeout else 'timeout' if timed_out or return_code == 124 else 'completed' if return_code == 0 and not parsed.get('is_error') else 'failed'
    resume_command = f"cd {shlex.quote(str(paths.root))} && claude --resume {returned_session_id} --add-dir {shlex.quote(repo)}" if returned_session_id and repo else (f"cd {shlex.quote(str(paths.root))} && claude --resume {returned_session_id}" if returned_session_id else session.get('resume_command', ''))
    result = {
        'project': project,
        'stage': stage,
        'session_id': returned_session_id,
        'workspace_root': str(paths.root),
        'repo_path': repo,
        'instruction': instruction,
        'started_at': started,
        'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': status,
        'return_code': return_code,
        'stdout': stdout[-20000:],
        'raw_stdout': raw_stdout[-30000:],
        'stderr': stderr[-6000:],
        'claude_json': parsed,
        'claude_events_tail': json_events[-40:],
        'prompt_path': str(prompt_file),
        'session_key': session_key,
        'launch_command': launch_command,
        'resume_command': resume_command,
        'startup_silent_timeout': startup_silent_timeout,
        'no_event_stream_timeout': no_event_stream_timeout,
        'partial_output_overflow': partial_output_overflow,
        'max_partial_output_bytes': max_partial_output_bytes,
        'tool_policy_guard': tool_policy_report,
    }
    session.update({
        'status': status,
        'updated_at': result['finished_at'],
        'last_stage': stage,
        'last_instruction': instruction,
        'last_return_code': return_code,
        'last_result_path': str(keyed_state_path(paths, 'claude_project_session_last_result', session_key)),
        'repo_path': repo,
        'session_id': returned_session_id,
        'resume_command': resume_command,
        'session_reset_reason': reset_reason or session.get('session_reset_reason', ''),
    })
    if return_code == 0 and returned_session_id:
        session['claude_session_created'] = True
        session['last_success_at'] = result['finished_at']
    guard_report = route_guard_report if isinstance(route_guard_report, dict) else {}
    if not (isinstance(guard_report, dict) and guard_report.get('repaired')):
        try:
            guard_report = guard_selected_base_route(project, source_stage=stage)
        except Exception as exc:
            guard_report = {'status': 'error', 'error': str(exc), 'source_stage': stage, 'repaired': False}
    if isinstance(guard_report, dict) and guard_report.get('repaired'):
        status = 'blocked_selected_base_route_guard'
        return_code = 2 if return_code == 0 else return_code
        result['status'] = status
        result['return_code'] = return_code
        result['selected_base_route_guard'] = guard_report
        result['stdout'] = (str(result.get('stdout') or '') + '\n\n[selected-base route guard] Restored current selected-base identity from trusted full reference reproduction audit; legacy/control route overwrite was blocked.').strip()[-20000:]
        session['last_return_code'] = return_code
        session['status'] = status
    else:
        result['selected_base_route_guard'] = guard_report
    if tool_policy_tripped:
        result['tool_policy_guard'] = tool_policy_report
        if 'trajectory supervisor recursion' in str(tool_policy_report.get('reason') or ''):
            result['stdout'] = (str(result.get('stdout') or '') + '\n\n[tool policy guard] Blocked nested trajectory supervisor launch; finish the assigned trajectory item instead of spawning another supervisor.').strip()[-20000:]
        elif is_current_find_artifact_policy_reason(tool_policy_report.get('reason')):
            result['stdout'] = (str(result.get('stdout') or '') + '\n\n[tool policy guard] Blocked unsafe current-Find artifact writing. This is recoverable: rerun the current-Find repair prompt and author or repair per-paper deep-read fragments with Claude file tools plus complete ideas.json/plans.json Write artifacts after reading full-text files; The workflow will merge validated fragments.').strip()[-20000:]
        elif is_current_find_gate_state_policy_reason(tool_policy_report.get('reason')):
            result['stdout'] = (str(result.get('stdout') or '') + '\n\n[tool policy guard] Blocked direct current-Find gate/state edits. This Claude turn was terminated; wrapper writes state files only after machine validation passes.').strip()[-20000:]
        else:
            result['stdout'] = (str(result.get('stdout') or '') + '\n\n[tool policy guard] Blocked naked experiment launch; relaunch through scripts/launch_experiment_run.py.').strip()[-20000:]
    result_path = keyed_state_path(paths, 'claude_project_session_last_result', session_key)
    save_json(session_path(paths, session_key), session)
    save_json(result_path, result)
    if session_key == 'main':
        save_json(paths.state / 'claude_project_session_last_result.json', result)
    with history_path(paths, session_key).open('a', encoding='utf-8') as handle:
        handle.write(f"\n## {result['finished_at']} | {stage} | {status}\n\n")
        handle.write(f"Instruction:\n\n```text\n{instruction}\n```\n\n")
        if stdout:
            handle.write("Claude response:\n\n" + stdout.strip() + "\n\n")
        if stderr and return_code != 0:
            handle.write("stderr:\n\n```text\n" + stderr[-3000:] + "\n```\n\n")
    mark_agent(project, agent_id, status='done' if status == 'completed' else status, current_step=f"Claude Code {status}", result={'return_code': return_code, 'result_path': str(result_path), 'selected_base_route_guard': result.get('selected_base_route_guard', {}), 'tool_policy_guard': result.get('tool_policy_guard', {})})
    emit(f"claude: saved session result to {result_path}")
    emit(f"claude: status={status} return_code={return_code}")
    return result


def status(project: str, session_key: str = 'main') -> dict[str, Any]:
    paths = build_paths(project)
    session = load_json(session_path(paths, session_key), {})
    if not isinstance(session, dict) or not session:
        session = ensure_session(project, resolve_session_repo_path(paths, 'status', ''), session_key=session_key)
    last = load_json(keyed_state_path(paths, 'claude_project_session_last_result', session_key), {})
    transcript = read_text(history_path(paths, session_key), 20000)
    return {'session': session, 'last_result': last, 'transcript_tail': transcript[-12000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description='Persistent project-level Claude Code session for TASTE.')
    parser.add_argument('--project', required=True)
    parser.add_argument('--stage', default='manual')
    parser.add_argument('--message', default='')
    parser.add_argument('--message-file', default='', help='Read the TASTE instruction from a UTF-8 text file to avoid argv length limits.')
    parser.add_argument('--timeout-sec', type=int, default=int(os.environ.get('CLAUDE_SESSION_TIMEOUT_SEC', '14400')))
    parser.add_argument('--agent-id', default='main')
    parser.add_argument('--session-key', default='', help='Optional explicit Claude session namespace; defaults to agent/stage isolation.')
    parser.add_argument('--repo-path', default='', help='Repository/work directory to expose to Claude; fresh-base stages auto-select the Find-selected repo when omitted.')
    parser.add_argument('--status-only', action='store_true')
    parser.add_argument('--no-resume', action='store_true')
    args = parser.parse_args()
    if args.session_key:
        args.agent_id = args.session_key
    if args.status_only:
        print(json.dumps(json_safe(status(args.project, session_key=session_key_for(args.agent_id, args.stage))), ensure_ascii=False, indent=2))
        return 0
    message = args.message
    if args.message_file:
        message = Path(args.message_file).read_text(encoding='utf-8', errors='replace')
    if not message.strip():
        raise SystemExit('--message is required unless --status-only')
    result = run_claude(args.project, message, args.stage, args.timeout_sec, resume=not args.no_resume, agent_id=args.agent_id, repo_path=args.repo_path)
    if result.get('return_code') != 0 and result.get('stderr'):
        print(str(result.get('stderr'))[-3000:], flush=True)
    return 0 if result.get('return_code') == 0 else int(result.get('return_code') or 1)


if __name__ == '__main__':
    raise SystemExit(main())
