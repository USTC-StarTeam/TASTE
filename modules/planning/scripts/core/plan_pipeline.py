from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
PUBLIC_FINAL_PLAN_ARTIFACT = "plan.md"

PLAN_CANDIDATE_FORMAT = """## 1. <plan title>

- **Plan ID**: `<plan_id>`
- **Idea ID**: `<idea_id>`
- **Latest Version**: `<version_id>`
- **Selected for Execution**: true/false
- **Completed**: true/false

### New Method
<human-readable method summary>

### Method Details
<implementation-level mechanism; omit this section only when empty>

### Initial Experiment
<minimum executable experiment>

### 启发来源
- [paper or web source title](<https://example.com>)

### Step-by-step Plan
1. <step>

### Risks
- <risk>

### Metrics
- <metric>
"""

REQUIRED_SECTIONS = (
    "### New Method",
    "### Initial Experiment",
    "### 启发来源",
    "### Step-by-step Plan",
    "### Risks",
    "### Metrics",
)


@dataclass
class PlanningConfig:
    research_interest: str = ""
    researcher_profile: str = ""


@dataclass
class PlanRequest:
    run_id: str
    idea_ids: list[str]
    repair_rounds: int = 3


@dataclass
class PlanPolishRequest:
    run_id: str
    plan_id: str
    version_id: str = ""
    rounds: int = 1


class JobCancelled(RuntimeError):
    pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text), encoding="utf-8")


def update_manifest(directory: Path, stage: str) -> None:
    write_json(directory / "manifest.json", {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stages": [stage],
        "public_final_artifact": PUBLIC_FINAL_PLAN_ARTIFACT,
    })


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_public_entrypoint() -> None:
    if os.environ.get("PLANNING_PUBLIC_ENTRYPOINT_ACTIVE") != "1":
        raise RuntimeError("Planning backend functions must be called through modules/planning/main.py")


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise JobCancelled("Task cancelled by user.")


def _use_claude_code_backend() -> bool:
    backend = str(os.environ.get("PLAN_BACKEND") or "claude_code").strip().lower()
    return backend in {"claude", "claude_code", "claudecode"}


def _find_claude_executable() -> Path | None:
    configured = str(os.environ.get("PLANNING_CLAUDE_PATH") or os.environ.get("CLAUDE_PATH") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    found = shutil.which("claude")
    return Path(found).resolve() if found else None


def _claude_env() -> dict[str, str]:
    env = os.environ.copy()
    workspace_root = Path(__file__).resolve().parents[4]
    # Do not let a module-local Claude process discover the enclosing TASTE repository.
    env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        filter(None, (str(workspace_root), env.get("GIT_CEILING_DIRECTORIES", "")))
    )
    return env


def _safe_label(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "").strip()).strip("-").lower()[:80] or "claude"


def _claude_run_root(directory: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = directory / "claude_runs" / f"{stamp}_{_safe_label(label)}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _run_claude_markdown_writer(
    prompt: str,
    directory: Path,
    target_path: Path,
    label: str,
    log: LogFn,
) -> dict[str, Any]:
    executable = _find_claude_executable()
    run_root = _claude_run_root(directory, label)
    write_text(run_root / "prompt.md", prompt)
    before_text = target_path.read_text(encoding="utf-8", errors="replace") if target_path.is_file() else ""
    before_mtime = target_path.stat().st_mtime_ns if target_path.is_file() else 0
    if executable is None:
        meta = {"status": "claude_not_found", "run_dir": str(run_root), "target_path": str(target_path)}
        write_json(run_root / "result.json", meta)
        raise RuntimeError("Claude Code is required to generate or revise plan.md, but the claude executable was not found.")
    command = [
        str(executable), "-p", "--output-format", "json",
        "--permission-mode", "acceptEdits", "--allowedTools", "Read,Write,Edit",
        "--no-session-persistence", "--add-dir", str(directory),
    ]
    model = str(os.environ.get("PLANNING_CLAUDE_MODEL") or "sonnet").strip()
    effort = str(os.environ.get("PLANNING_CLAUDE_EFFORT") or "high").strip()
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    timeout = int(float(os.environ.get("PLANNING_CLAUDE_TIMEOUT_SEC") or "900"))
    meta: dict[str, Any] = {
        "status": "started", "command": command, "model": model, "effort": effort,
        "timeout_sec": timeout, "run_dir": str(run_root), "target_path": str(target_path),
    }
    write_json(run_root / "command.json", meta)
    try:
        proc = subprocess.run(command, input=prompt, cwd=directory, env=_claude_env(), text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        meta.update({"status": "failed_to_launch", "error": str(exc)})
        write_json(run_root / "result.json", meta)
        raise RuntimeError(f"Claude Code failed while writing plan.md: {exc}") from exc
    write_text(run_root / "stdout.json", proc.stdout or "")
    if proc.stderr:
        write_text(run_root / "stderr.log", proc.stderr)
    meta.update({"returncode": proc.returncode, "stdout_chars": len(proc.stdout or ""), "stderr_chars": len(proc.stderr or "")})
    if proc.returncode != 0:
        meta["status"] = "nonzero_returncode"
        write_json(run_root / "result.json", meta)
        raise RuntimeError(f"Claude Code returned {proc.returncode} while writing plan.md.")
    after_text = target_path.read_text(encoding="utf-8", errors="replace") if target_path.is_file() else ""
    after_mtime = target_path.stat().st_mtime_ns if target_path.is_file() else 0
    if not after_text.strip() or (after_text == before_text and after_mtime <= before_mtime):
        meta["status"] = "target_file_not_written"
        write_json(run_root / "result.json", meta)
        raise RuntimeError("Claude Code completed without writing the required plan.md file.")
    meta["status"] = "ok_file_written"
    write_json(run_root / "result.json", {"meta": meta, "source": PUBLIC_FINAL_PLAN_ARTIFACT})
    log(f"Claude Code wrote {target_path.name}: {label}")
    return meta


def _json_span(text: str) -> str:
    value = str(text or "").strip()
    first = value.find("{")
    last = value.rfind("}")
    if first < 0 or last < first:
        raise ValueError("Claude output did not contain a JSON object")
    return value[first:last + 1]


def _extract_claude_json(stdout: str) -> dict[str, Any]:
    outer = json.loads(_json_span(stdout))
    if isinstance(outer, dict):
        for key in ("structured_output", "result", "content", "output"):
            value = outer.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and "{" in value:
                parsed = json.loads(_json_span(value))
                if isinstance(parsed, dict):
                    return parsed
        return outer
    raise ValueError("Claude output did not contain a JSON object")


def _run_claude_json(prompt: str, schema: dict[str, Any], directory: Path, label: str, log: LogFn) -> tuple[dict[str, Any], dict[str, Any]]:
    executable = _find_claude_executable()
    run_root = _claude_run_root(directory, label)
    write_text(run_root / "prompt.md", prompt)
    write_json(run_root / "schema.json", schema)
    if executable is None:
        raise RuntimeError("Claude Code is required for Planning selection, but the claude executable was not found.")
    command = [
        str(executable), "-p", "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--no-session-persistence", "--add-dir", str(directory),
    ]
    model = str(os.environ.get("PLANNING_CLAUDE_MODEL") or "sonnet").strip()
    effort = str(os.environ.get("PLANNING_CLAUDE_EFFORT") or "high").strip()
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    timeout = int(float(os.environ.get("PLANNING_CLAUDE_TIMEOUT_SEC") or "900"))
    meta: dict[str, Any] = {"status": "started", "command": command, "run_dir": str(run_root), "timeout_sec": timeout}
    write_json(run_root / "command.json", meta)
    proc = subprocess.run(command, input=prompt, cwd=directory, env=_claude_env(), text=True, capture_output=True, timeout=timeout)
    write_text(run_root / "stdout.json", proc.stdout or "")
    if proc.stderr:
        write_text(run_root / "stderr.log", proc.stderr)
    if proc.returncode != 0:
        meta.update({"status": "nonzero_returncode", "returncode": proc.returncode})
        write_json(run_root / "result.json", meta)
        raise RuntimeError(f"Claude Code returned {proc.returncode} during Planning selection.")
    payload = _extract_claude_json(proc.stdout or "")
    meta.update({"status": "ok", "returncode": 0})
    write_json(run_root / "result.json", {"meta": meta, "payload": payload})
    log(f"Claude Code completed structured Planning decision: {label}")
    return payload, meta


def _idea_key(idea: dict[str, Any]) -> str:
    return str(idea.get("id") or idea.get("idea_id") or idea.get("title") or "").strip()


def _approved_for_planning(idea: Any) -> bool:
    if not isinstance(idea, dict):
        return False
    status = str(idea.get("status") or idea.get("recommendation") or "").strip().lower()
    if status in {"deleted", "rejected", "reject", "archived", "pending"}:
        return False
    return bool(
        idea.get("approved") is True or idea.get("approved_for_planning") is True or idea.get("pursue") is True
        or status == "approved" or "approved" in status or "pursue" in status
    )


def _plan_id_for_idea(idea: dict[str, Any]) -> str:
    return f"plan-{_idea_key(idea) or 'unknown'}"


def _ideas_revision(ideas: list[dict[str, Any]]) -> str:
    normalized = json.dumps(ideas, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_ideas_data(directory: Path, run_id: str) -> dict[str, Any]:
    data = read_json(directory / "ideas.json", {})
    if not isinstance(data, dict) or str(data.get("run_id") or "").strip() != str(run_id or "").strip():
        raise ValueError("Planning ideas.json is missing or does not match the explicit input run_id.")
    return data


def _load_plans_data(directory: Path, run_id: str) -> dict[str, Any]:
    data = read_json(directory / "plans.json", {})
    if not isinstance(data, dict) or str(data.get("run_id") or "").strip() != str(run_id or "").strip():
        raise ValueError("Planning plans.json is missing or does not match the explicit input run_id.")
    return data


def _clean_meta(value: str) -> str:
    return str(value or "").strip().strip("`").strip()


def _metadata_value(block: str, label: str) -> str:
    match = re.search(rf"^-\s+\*\*{re.escape(label)}\*\*:\s*(.*?)\s*$", block, flags=re.MULTILINE | re.IGNORECASE)
    return _clean_meta(match.group(1)) if match else ""


def _section_text(block: str, heading: str) -> str:
    match = re.search(rf"^{re.escape(heading)}\s*$\n(.*?)(?=^###\s|^##\s|\Z)", block, flags=re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _list_items(text: str) -> list[str]:
    rows: list[str] = []
    for line in str(text or "").splitlines():
        match = re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)(.+?)\s*$", line)
        if match and match.group(1).strip():
            rows.append(match.group(1).strip())
    return rows


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "selected", "complete", "completed"}


def _parse_plan_markdown(markdown: str) -> dict[str, Any]:
    text = str(markdown or "").replace("\r\n", "\n")
    heading_matches = list(re.finditer(r"^##\s+(\d+)\.\s+(.+?)\s*$", text, flags=re.MULTILINE))
    plans: list[dict[str, Any]] = []
    for index, match in enumerate(heading_matches):
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(text)
        block = text[match.start():end].strip()
        plans.append({
            "order": int(match.group(1)),
            "title": match.group(2).strip(),
            "plan_id": _metadata_value(block, "Plan ID"),
            "idea_id": _metadata_value(block, "Idea ID"),
            "latest_version": _metadata_value(block, "Latest Version"),
            "selected_for_execution": _parse_bool(_metadata_value(block, "Selected for Execution")),
            "completed": _parse_bool(_metadata_value(block, "Completed")),
            "sections": {heading.removeprefix("### "): _section_text(block, heading) for heading in REQUIRED_SECTIONS},
            "method_details": _section_text(block, "### Method Details"),
            "block": block,
        })
    selected_start = re.search(r"^## Selected Plan for Execution\s*$", text, flags=re.MULTILINE)
    selected: dict[str, str] = {}
    if selected_start:
        selected_end = heading_matches[0].start() if heading_matches else len(text)
        block = text[selected_start.start():selected_end]
        selected = {
            "plan_id": _metadata_value(block, "Plan ID"),
            "idea_id": _metadata_value(block, "Idea ID"),
            "title": _metadata_value(block, "Title"),
        }
    return {"plans": plans, "selected": selected, "has_selected_section": bool(selected_start)}


def _bare_urls(text: str) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"https?://[^\s<>)]+", str(text or "")):
        prefix = text[max(0, match.start() - 3):match.start()]
        previous = text[match.start() - 1:match.start()]
        if prefix.endswith("](<") or previous == "<":
            continue
        out.append(match.group(0))
    return out


def _plan_markdown_audit(markdown: str, plans: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[str] = []
    text = str(markdown or "")
    parsed = _parse_plan_markdown(text)
    rows = parsed["plans"]
    expected = [row for row in plans if isinstance(row, dict)]
    if not text.lstrip().startswith("# Research Plans"):
        issues.append("missing_top_heading")
    if len(rows) != len(expected):
        issues.append(f"candidate_heading_count:{len(rows)}!={len(expected)}")
    expected_by_plan = {str(row.get("plan_id") or "").strip(): row for row in expected}
    seen_plan_ids: set[str] = set()
    seen_idea_ids: set[str] = set()
    for position, row in enumerate(rows, 1):
        plan_id = str(row.get("plan_id") or "").strip()
        idea_id = str(row.get("idea_id") or "").strip()
        if row.get("order") != position:
            issues.append(f"candidate_order:{row.get('order')}!={position}")
        if not plan_id or plan_id in seen_plan_ids:
            issues.append(f"invalid_plan_id:{plan_id or position}")
        if not idea_id or idea_id in seen_idea_ids:
            issues.append(f"invalid_idea_id:{idea_id or position}")
        seen_plan_ids.add(plan_id)
        seen_idea_ids.add(idea_id)
        expected_row = expected_by_plan.get(plan_id)
        if expected_row is None:
            issues.append(f"unknown_plan_id:{plan_id or position}")
        elif str(expected_row.get("idea_id") or "").strip() != idea_id:
            issues.append(f"plan_idea_mismatch:{plan_id}")
        if not str(row.get("title") or "").strip():
            issues.append(f"missing_title:{plan_id or position}")
        if not str(row.get("latest_version") or "").strip():
            issues.append(f"missing_latest_version:{plan_id or position}")
        block = str(row.get("block") or "")
        selected_value = _metadata_value(block, "Selected for Execution").lower()
        completed_value = _metadata_value(block, "Completed").lower()
        if selected_value not in {"true", "false"}:
            issues.append(f"invalid_selected_metadata:{plan_id or position}")
        if completed_value not in {"true", "false"}:
            issues.append(f"invalid_completed_metadata:{plan_id or position}")
        section_positions = [block.find(heading) for heading in REQUIRED_SECTIONS]
        for heading, section_position in zip(REQUIRED_SECTIONS, section_positions):
            if section_position < 0:
                issues.append(f"missing_candidate_section:{plan_id}:{heading.removeprefix('### ')}")
            elif not str((row.get("sections") or {}).get(heading.removeprefix("### ")) or "").strip():
                issues.append(f"empty_candidate_section:{plan_id}:{heading.removeprefix('### ')}")
            if block.count(heading) != 1:
                issues.append(f"candidate_section_count:{plan_id}:{heading.removeprefix('### ')}={block.count(heading)}")
        if all(value >= 0 for value in section_positions) and section_positions != sorted(section_positions):
            issues.append(f"invalid_candidate_section_order:{plan_id}")
        method_position = block.find("### Method Details")
        if method_position >= 0 and section_positions[0] >= 0 and section_positions[1] >= 0 and not (section_positions[0] < method_position < section_positions[1]):
            issues.append(f"invalid_method_details_position:{plan_id}")
        steps = str((row.get("sections") or {}).get("Step-by-step Plan") or "")
        step_lines = [line.strip() for line in steps.splitlines() if re.match(r"^\s*\d+\.\s+", line)]
        if not step_lines:
            issues.append(f"missing_ordered_steps:{plan_id}")
        if any(re.match(r"^\d+\.\s+\d+\.\s+", line) for line in step_lines):
            issues.append(f"double_numbered_steps:{plan_id}")
        normalized_steps = [re.sub(r"^\d+\.\s+", "", line).strip().lower() for line in step_lines]
        if len(normalized_steps) != len(set(normalized_steps)):
            issues.append(f"duplicate_steps:{plan_id}")
        for section_name in ("Risks", "Metrics", "启发来源"):
            if not _list_items(str((row.get("sections") or {}).get(section_name) or "")):
                issues.append(f"missing_list_items:{plan_id}:{section_name}")
    expected_ids = {str(row.get("plan_id") or "").strip() for row in expected}
    if seen_plan_ids != expected_ids:
        issues.append("candidate_plan_ids_do_not_match_input")
    selected_rows = [row for row in rows if row.get("selected_for_execution") is True]
    selected = parsed["selected"]
    if len(selected_rows) > 1:
        issues.append("multiple_selected_plans")
    if selected_rows:
        target = selected_rows[0]
        if not parsed["has_selected_section"]:
            issues.append("missing_selected_plan_section")
        elif selected.get("plan_id") != target.get("plan_id") or selected.get("idea_id") != target.get("idea_id"):
            issues.append("selected_plan_section_mismatch")
        if not target.get("completed"):
            issues.append("selected_plan_not_completed")
        if any(row.get("completed") for row in rows if row.get("plan_id") != target.get("plan_id")):
            issues.append("nonselected_plan_marked_completed")
    elif parsed["has_selected_section"]:
        issues.append("selected_section_without_selected_candidate")
    elif any(row.get("completed") for row in rows):
        issues.append("completed_plan_without_selection")
    if text.count("```") % 2:
        issues.append("unbalanced_fenced_code_blocks")
    if text.count("$$") % 2:
        issues.append("unbalanced_display_math_delimiters")
    if len(re.findall(r"(?<!\\)(?<!\$)\$(?!\$)", text)) % 2:
        issues.append("unbalanced_inline_math_delimiters")
    if re.search(r"\]\(https?://", text):
        issues.append("web_links_must_use_angle_brackets")
    bare_urls = _bare_urls(text)
    if bare_urls:
        issues.append("bare_web_urls:" + ", ".join(bare_urls[:3]))
    placeholder_pattern = r"<(?:selected_plan_id|selected_idea_id|plan_id|idea_id|plan title|selected plan title|human-readable method summary|implementation-level mechanism[^>]*|minimum executable experiment|paper or web source title|step|risk|metric)>"
    if "example.com" in text or re.search(placeholder_pattern, text, flags=re.IGNORECASE):
        issues.append("unresolved_markdown_placeholder")
    for forbidden in ("Evaluation / Repair Rounds", "Version History"):
        if forbidden in text:
            issues.append(f"audit_history_in_public_markdown:{forbidden}")
    return {
        "status": "pass" if not issues else "fail",
        "issues": list(dict.fromkeys(issues)),
        "checked": [
            "candidate_ids", "candidate_sections", "nonempty_content", "ordered_steps",
            "duplicate_steps", "selection", "math_delimiters", "web_citations", "audit_history",
        ],
    }


def _expected_plan_rows(ideas: list[dict[str, Any]], version_id: str = "v1") -> list[dict[str, Any]]:
    return [
        {"plan_id": _plan_id_for_idea(idea), "idea_id": _idea_key(idea), "title": str(idea.get("title") or "Untitled"), "latest_version": version_id}
        for idea in ideas
    ]


def _compact_versions(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value if isinstance(value, list) else [], 1):
        source = item if isinstance(item, dict) else {}
        version_id = str(source.get("version_id") or source.get("version") or f"v{index}").strip()
        rows.append({key: source.get(key) for key in ("version_id", "source", "created_at", "markdown_sha256") if source.get(key) not in (None, "")})
        rows[-1]["version_id"] = version_id
    return rows


def _next_version_id(versions: list[dict[str, Any]]) -> str:
    numbers = []
    for row in versions:
        match = re.fullmatch(r"v(\d+)", str(row.get("version_id") or ""))
        if match:
            numbers.append(int(match.group(1)))
    return f"v{max(numbers, default=0) + 1}"


def _markdown_sha256(markdown: str) -> str:
    return hashlib.sha256(str(markdown or "").encode("utf-8")).hexdigest()


def _projection_from_markdown(
    markdown: str,
    run_id: str,
    expected: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
    source: str,
    selection_actor: str = "",
    selection_rationale: str = "",
    repair_rounds: int = 0,
) -> dict[str, Any]:
    audit = _plan_markdown_audit(markdown, expected)
    if audit["status"] != "pass":
        raise ValueError(f"plan.md did not pass publication audit: {audit['issues']}")
    parsed = _parse_plan_markdown(markdown)
    previous_rows = {
        str(row.get("plan_id") or "").strip(): row
        for row in (previous or {}).get("plans", []) if isinstance(row, dict)
    }
    digest = _markdown_sha256(markdown)
    plans: list[dict[str, Any]] = []
    for row in parsed["plans"]:
        plan_id = str(row["plan_id"])
        old = previous_rows.get(plan_id, {})
        versions = _compact_versions(old.get("versions"))
        markdown_version = str(row.get("latest_version") or "").strip()
        if not versions:
            versions = [{"version_id": markdown_version, "source": source, "created_at": _now()}]
        elif markdown_version != str(versions[-1].get("version_id") or ""):
            versions.append({"version_id": markdown_version, "source": source, "created_at": _now()})
        latest = versions[-1]
        latest["markdown_sha256"] = digest
        plans.append({
            "plan_id": plan_id,
            "idea_id": str(row["idea_id"]),
            "order": int(row["order"]),
            "selected_for_execution": bool(row["selected_for_execution"]),
            "completed": bool(row["completed"]),
            "versions": versions,
        })
    selected = next((row for row in plans if row["selected_for_execution"]), None)
    selected_plan_id = str((selected or {}).get("plan_id") or "")
    selected_idea_id = str((selected or {}).get("idea_id") or "")
    selected_by = selection_actor if selected_plan_id and selection_actor else str((previous or {}).get("selected_by") or "")
    return {
        "schema_version": "taste.plans_projection.v2",
        "run_id": run_id,
        "source": "plan_md_projection",
        "machine_projection_from": PUBLIC_FINAL_PLAN_ARTIFACT,
        "public_final_artifact": PUBLIC_FINAL_PLAN_ARTIFACT,
        "planned_idea_ids": [row["idea_id"] for row in plans],
        "idea_revision": str((previous or {}).get("idea_revision") or ""),
        "plans": plans,
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
        "selected_by": selected_by,
        "selection_rationale": selection_rationale or str((previous or {}).get("selection_rationale") or ""),
        "selection_issue": "" if selected_plan_id else "missing_selected_plan",
        "execution_policy": {
            "status": "selected_plan_only" if selected_plan_id else "missing_selected_plan",
            "downstream_consumes": "selected_plan_id",
            "candidate_backlog_policy": "Non-selected plans are review-only backlog.",
        },
        "plan_markdown_generation": {
            "source": source,
            "public_final_artifact": PUBLIC_FINAL_PLAN_ARTIFACT,
            "repair_rounds": int(repair_rounds),
            "sha256": digest,
            "audit": audit,
            "updated_at": _now(),
        },
        "artifact_policy": {
            "public_plan_body": PUBLIC_FINAL_PLAN_ARTIFACT,
            "plans_json_role": "metadata_and_selection_projection_without_plan_body",
            "experiment_plan_role": "selected_plan_contract_for_downstream_modules",
            "taste_plan_bridge_role": "paths_and_selection_index_only",
        },
    }


def _selected_contract(markdown: str, selected_plan_id: str) -> dict[str, Any]:
    row = next((item for item in _parse_plan_markdown(markdown)["plans"] if item.get("plan_id") == selected_plan_id), None)
    if not isinstance(row, dict):
        return {}
    sections = row.get("sections") if isinstance(row.get("sections"), dict) else {}
    return {
        "plan_id": row.get("plan_id", ""),
        "idea_id": row.get("idea_id", ""),
        "title": row.get("title", ""),
        "new_method": sections.get("New Method", ""),
        "hypothesis": sections.get("New Method", ""),
        "method_details": row.get("method_details", ""),
        "initial_experiment": sections.get("Initial Experiment", ""),
        "inspired_by": _list_items(sections.get("启发来源", "")),
        "steps": _list_items(sections.get("Step-by-step Plan", "")),
        "risks": _list_items(sections.get("Risks", "")),
        "metrics": _list_items(sections.get("Metrics", "")),
        "plan_markdown_path": PUBLIC_FINAL_PLAN_ARTIFACT,
    }


def _build_experiment_plan(data: dict[str, Any], markdown: str, markdown_path: Path) -> dict[str, Any]:
    selected_plan_id = str(data.get("selected_plan_id") or "")
    contract = _selected_contract(markdown, selected_plan_id) if selected_plan_id else {}
    ready = bool(contract)
    return {
        "run_id": data.get("run_id", ""),
        "source": "plan_md_projection",
        "status": "selected_plan_ready" if ready else "blocked_missing_selected_plan",
        "execution_ready": ready,
        "takeover_ready": ready,
        "public_final_artifact": PUBLIC_FINAL_PLAN_ARTIFACT,
        "plan_markdown_path": str(markdown_path),
        "plan_markdown_sha256": _markdown_sha256(markdown),
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": str(data.get("selected_idea_id") or ""),
        "selected_plan_contract": contract,
        "current_find_plan_count": len(data.get("plans", [])),
        "selection_issue": "" if ready else "missing_selected_plan",
        "next_required_action": "environment_stage_claude_code_base_selection" if ready else "select_exactly_one_plan",
        "execution_policy": data.get("execution_policy", {}),
        "downstream_policy": "Downstream modules consume only selected_plan_id and the canonical plan.md-derived selected contract.",
    }


def _build_taste_plan_bridge(data: dict[str, Any], markdown_path: Path, experiment_plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "plan_md_projection",
        "run_id": data.get("run_id", ""),
        "public_final_artifact": PUBLIC_FINAL_PLAN_ARTIFACT,
        "plans_json_path": str(markdown_path.with_name("plans.json")),
        "plan_markdown_path": str(markdown_path),
        "experiment_plan_json_path": str(markdown_path.with_name("experiment_plan.json")),
        "selected_plan_id": data.get("selected_plan_id", ""),
        "selected_idea_id": data.get("selected_idea_id", ""),
        "selected_by": data.get("selected_by", ""),
        "selected_plan_contract_status": experiment_plan.get("status", ""),
        "selected_plan_contract_path": str(markdown_path.with_name("experiment_plan.json")),
        "execution_policy": data.get("execution_policy", {}),
    }


def _write_plan_outputs(directory: Path, data: dict[str, Any], markdown: str) -> None:
    markdown_path = directory / PUBLIC_FINAL_PLAN_ARTIFACT
    if not markdown_path.is_file() or markdown_path.read_text(encoding="utf-8", errors="replace") != markdown:
        raise ValueError("Canonical plan.md changed after validation.")
    experiment_plan = _build_experiment_plan(data, markdown, markdown_path)
    bridge = _build_taste_plan_bridge(data, markdown_path, experiment_plan)
    write_json(directory / "plans.json", data)
    write_json(directory / "experiment_plan.json", experiment_plan)
    write_json(directory / "taste_plan_bridge.json", bridge)


def _plan_markdown_prompt(ideas: list[dict[str, Any]], config: PlanningConfig, directory: Path) -> str:
    target = (directory / PUBLIC_FINAL_PLAN_ARTIFACT).resolve()
    ideas_path = (directory / "ideas.json").resolve()
    candidates = [
        {
            "plan_id": _plan_id_for_idea(idea),
            "idea_id": _idea_key(idea),
            "title": idea.get("title", "Untitled"),
            "new_method": idea.get("new_method") or idea.get("hypothesis") or "",
            "method_details": idea.get("method_details") or idea.get("mechanism") or idea.get("rationale") or "",
            "initial_experiment": idea.get("initial_experiment") or idea.get("experiment_design") or "",
            "inspired_by": idea.get("inspired_by") or idea.get("supporting_papers") or [],
        }
        for idea in ideas
    ]
    return f"""You are the TASTE Planning Claude Code writer.

Read the explicit approved-Idea input at `{ideas_path}`. The normalized candidates are also included below. Write the complete final user-facing research plan directly to `{target}` using Write/Edit. Do not return the Markdown as chat output and do not create another plan file.

`plan.md` is the canonical Planning artifact. Do not write or edit plans.json, experiment_plan.json, taste_plan_bridge.json, ideas.json, or any file except `{target}`.

Start with `# Research Plans`, then use exactly this heading structure and order for every candidate:
```markdown
{PLAN_CANDIDATE_FORMAT.rstrip()}
```

Rules:
- Do not write `## Selected Plan for Execution` during candidate generation; every candidate starts unselected and incomplete.
- Preserve every supplied plan_id and idea_id exactly. Number candidates from 1 without duplicated numbering.
- Write concrete, executable scientific content. Include implementation mechanism, minimum experiment, baselines/ablations, metrics and thresholds, bad-case/counterexample checks, risks, stop conditions, and evidence needed before claims.
- Planning may state requirements but must not claim a repository, local path, dataset path, command, metric result, or environment has already been selected or validated.
- Use `[source title](<https://example.com>)` for every web reference. Never leave a bare URL.
- Use balanced `$...$` or `$$...$$` for mathematics.
- Do not include JSON, evaluation history, version history, raw prompts, or duplicated steps.
- Before finishing, read `{target}` back and verify candidate count, IDs, non-empty sections, step numbering, citations, math delimiters, and duplicate content. Repair the same file until correct.

Research interest:
{config.research_interest}

Researcher profile:
{config.researcher_profile}

Approved candidates:
```json
{json.dumps(candidates, ensure_ascii=False, indent=2)}
```
"""


def _repair_markdown_prompt(
    directory: Path,
    round_index: int,
    issues: list[str],
    target_plan_id: str = "",
    target_version: str = "",
) -> str:
    target = (directory / PUBLIC_FINAL_PLAN_ARTIFACT).resolve()
    scope = f"Revise only candidate `{target_plan_id}` and its metadata; preserve every other candidate byte-for-byte where possible." if target_plan_id else "Review and improve every candidate while preserving all IDs, candidate order, and selection state."
    version_instruction = f" Set that candidate's `Latest Version` to `{target_version}`." if target_version else " Preserve every `Latest Version` value."
    link_instruction = ""
    if any("url" in issue.lower() or "link" in issue.lower() for issue in issues):
        link_instruction = """
Link repair is a mandatory acceptance condition for this round. Search the complete file for every `http://` and `https://`. Every occurrence must be inside one Markdown destination written exactly as `[descriptive title](<https://...>)`; for example, change `[Paper](https://example.org/paper)` to `[Paper](<https://example.org/paper>)`. Do not leave a URL in prose or in `[title](https://...)` form. Read the complete file again and repeat the search before finishing.
"""
    return f"""You are performing direct Planning repair round {round_index}.

Open and read the canonical plan at `{target}`. {scope}{version_instruction}
Edit that exact file in place. Do not create or edit any other file.

Make a substantive but concise repair: remove duplicated text/numbering, make the method and minimum experiment executable, make baseline/ablation/metric/failure checks precise, and fix Markdown links and math. Preserve the required headings and use `[source title](<https://...>)` for links.

Every candidate must keep this exact Markdown shape and heading level:
```markdown
{PLAN_CANDIDATE_FORMAT.rstrip()}
```
Use `## <number>. <title>` as the single candidate heading. Keep Plan ID and Idea ID only in the metadata bullets; never create a separate heading from either ID. Keep the required `###` sections exactly once and in the shown order. The optional `## Selected Plan for Execution` summary may appear only once immediately after `# Research Plans`.
{link_instruction}

The deterministic checker currently reports:
```json
{json.dumps(issues, ensure_ascii=False, indent=2)}
```

After editing, read the file back and verify IDs, headings, non-empty sections, ordered steps, citations, formulas, and absence of duplicates. Return only a short completion acknowledgement.
"""


def _selection_edit_prompt(directory: Path, plan_id: str) -> str:
    target = (directory / PUBLIC_FINAL_PLAN_ARTIFACT).resolve()
    return f"""Open `{target}` and update that canonical Markdown file in place so `{plan_id}` is the only selected execution plan.

Add or replace `## Selected Plan for Execution` immediately after `# Research Plans` with the selected plan ID, matching Idea ID/title, and the selected-plan-only policy. In every candidate metadata block set `Selected for Execution` true only for `{plan_id}` and false for all others; set `Completed` true for `{plan_id}`. Preserve all plan prose, IDs, candidate order, headings, citations, and formulas. Do not edit or create any other file. Read the file back and verify exactly one selected plan before finishing.
"""


def render_plan_markdown(plans: list[dict[str, Any]]) -> str:
    lines = ["# Research Plans", ""]
    for index, idea in enumerate(plans, 1):
        plan_id = str(idea.get("plan_id") or _plan_id_for_idea(idea))
        idea_id = str(idea.get("idea_id") or _idea_key(idea))
        title = str(idea.get("title") or "Untitled")
        method = str(idea.get("new_method") or idea.get("hypothesis") or "Method details must be supplied.")
        details = str(idea.get("method_details") or idea.get("mechanism") or "")
        experiment = str(idea.get("initial_experiment") or idea.get("experiment_design") or "Run a minimum baseline/candidate/ablation comparison.")
        lines.extend([
            f"## {index}. {title}", "",
            f"- **Plan ID**: `{plan_id}`", f"- **Idea ID**: `{idea_id}`", "- **Latest Version**: `v1`",
            "- **Selected for Execution**: false", "- **Completed**: false", "",
            "### New Method", method, "",
        ])
        if details:
            lines.extend(["### Method Details", details, ""])
        lines.extend([
            "### Initial Experiment", experiment, "",
            "### 启发来源", "- No external web source was supplied in the explicit Idea input.", "",
            "### Step-by-step Plan", "1. Implement the smallest testable candidate and a protocol-matched baseline.",
            "2. Run candidate, baseline, and ablation under identical seeds, data, metrics, and logging.",
            "3. Audit bad cases, counterexamples, and go/no-go evidence before promoting claims.", "",
            "### Risks", "- Required repository, data, environment, or protocol evidence may be unavailable.", "",
            "### Metrics", "- Compare the primary task metric, robustness slices, and runtime cost against the matched baseline.", "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def run_plan_at_directory(
    directory: Path,
    request: PlanRequest,
    config: PlanningConfig,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
) -> dict[str, Any]:
    _require_public_entrypoint()
    ideas_data = _load_ideas_data(directory, request.run_id)
    ideas = [dict(row) for row in ideas_data.get("ideas", []) if _approved_for_planning(row)]
    if not ideas:
        raise ValueError("Planning requires at least one explicitly approved Idea.")
    ids = [_idea_key(row) for row in ideas]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("Approved Planning Ideas must have unique non-empty IDs.")
    if request.idea_ids and set(request.idea_ids) != set(ids):
        raise ValueError("Planning must process every explicitly approved Idea in the Framework input bundle.")
    rounds = int(request.repair_rounds)
    if rounds < 0:
        raise ValueError("repair_rounds must be zero or greater.")
    target = directory / PUBLIC_FINAL_PLAN_ARTIFACT
    expected = _expected_plan_rows(ideas)
    _raise_if_cancelled(should_cancel)
    if _use_claude_code_backend():
        log(f"Asking Claude Code to write plan.md for {len(ideas)} approved Idea(s).")
        _run_claude_markdown_writer(_plan_markdown_prompt(ideas, config, directory), directory, target, "plan_md_initial", log)
        for round_index in range(1, rounds + 1):
            _raise_if_cancelled(should_cancel)
            current = target.read_text(encoding="utf-8", errors="replace")
            issues = list(_plan_markdown_audit(current, expected).get("issues") or [])
            _run_claude_markdown_writer(_repair_markdown_prompt(directory, round_index, issues), directory, target, f"plan_md_repair_{round_index}", log)
    else:
        write_text(target, render_plan_markdown(ideas))
    markdown = target.read_text(encoding="utf-8", errors="replace")
    data = _projection_from_markdown(markdown, request.run_id, expected, source="claude_code_direct" if _use_claude_code_backend() else "deterministic_test", repair_rounds=rounds)
    data["idea_revision"] = _ideas_revision(ideas)
    _write_plan_outputs(directory, data, markdown)
    update_manifest(directory, "plan")
    log("Plan stage complete: canonical plan.md written and audited.")
    return data


def polish_plan_at_directory(
    directory: Path,
    request: PlanPolishRequest,
    config: PlanningConfig,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
) -> dict[str, Any]:
    del config
    _require_public_entrypoint()
    previous = _load_plans_data(directory, request.run_id)
    target = directory / PUBLIC_FINAL_PLAN_ARTIFACT
    if not target.is_file():
        raise FileNotFoundError("Canonical plan.md is missing from the explicit Planning input.")
    expected = [dict(row) for row in previous.get("plans", []) if isinstance(row, dict)]
    target_row = next((row for row in expected if str(row.get("plan_id") or "") == request.plan_id), None)
    if target_row is None:
        raise ValueError(f"Plan not found: {request.plan_id}")
    target_version = _next_version_id(_compact_versions(target_row.get("versions")))
    rounds = int(request.rounds)
    if rounds < 1:
        raise ValueError("Polish rounds must be at least one.")
    for round_index in range(1, rounds + 1):
        _raise_if_cancelled(should_cancel)
        current = target.read_text(encoding="utf-8", errors="replace")
        issues = list(_plan_markdown_audit(current, expected).get("issues") or [])
        _run_claude_markdown_writer(_repair_markdown_prompt(directory, round_index, issues, request.plan_id, target_version), directory, target, f"polish_{request.plan_id}_{round_index}", log)
    markdown = target.read_text(encoding="utf-8", errors="replace")
    data = _projection_from_markdown(markdown, request.run_id, expected, previous=previous, source="claude_code_direct_polish", repair_rounds=rounds)
    ideas_data = _load_ideas_data(directory, request.run_id)
    relevant_ids = {str(row.get("idea_id") or "") for row in data.get("plans", []) if isinstance(row, dict)}
    data["idea_revision"] = _ideas_revision([dict(row) for row in ideas_data.get("ideas", []) if isinstance(row, dict) and _idea_key(row) in relevant_ids])
    _write_plan_outputs(directory, data, markdown)
    update_manifest(directory, "plan")
    return data


def _select_with_claude(directory: Path, plans: list[dict[str, Any]], config: PlanningConfig, log: LogFn) -> tuple[str, str]:
    prompt = f"""Read `{(directory / PUBLIC_FINAL_PLAN_ARTIFACT).resolve()}` and select exactly one existing plan for downstream execution. Compare novelty, feasibility, falsifiability, evidence requirements, failure analysis, and researcher fit. Do not merge plans or invent a plan.

Researcher profile:
{config.researcher_profile}

Allowed plans:
{json.dumps([{"plan_id": row.get("plan_id"), "idea_id": row.get("idea_id"), "title": row.get("title")} for row in plans], ensure_ascii=False, indent=2)}
"""
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"selected_plan_id": {"type": "string"}, "rationale": {"type": "string"}},
        "required": ["selected_plan_id", "rationale"],
    }
    payload, _meta = _run_claude_json(prompt, schema, directory, "select_plan", log)
    return str(payload.get("selected_plan_id") or "").strip(), str(payload.get("rationale") or "").strip()


def _apply_selection(directory: Path, run_id: str, plan_id: str, actor: str, rationale: str, log: LogFn) -> dict[str, Any]:
    previous = _load_plans_data(directory, run_id)
    expected = [dict(row) for row in previous.get("plans", []) if isinstance(row, dict)]
    if plan_id not in {str(row.get("plan_id") or "") for row in expected}:
        raise ValueError(f"Plan not found: {plan_id}")
    target = directory / PUBLIC_FINAL_PLAN_ARTIFACT
    _run_claude_markdown_writer(_selection_edit_prompt(directory, plan_id), directory, target, f"select_{plan_id}", log)
    markdown = target.read_text(encoding="utf-8", errors="replace")
    data = _projection_from_markdown(markdown, run_id, expected, previous=previous, source="claude_code_direct_selection", selection_actor=actor, selection_rationale=rationale)
    ideas_data = _load_ideas_data(directory, run_id)
    relevant_ids = {str(row.get("idea_id") or "") for row in data.get("plans", []) if isinstance(row, dict)}
    data["idea_revision"] = _ideas_revision([dict(row) for row in ideas_data.get("ideas", []) if isinstance(row, dict) and _idea_key(row) in relevant_ids])
    if data.get("selected_plan_id") != plan_id:
        raise ValueError("Claude Code did not mark the requested plan as the sole selected plan in plan.md.")
    _write_plan_outputs(directory, data, markdown)
    update_manifest(directory, "plan")
    return data


def select_plan_at_directory(directory: Path, run_id: str, config: PlanningConfig, *, log: LogFn = print) -> dict[str, Any]:
    _require_public_entrypoint()
    previous = _load_plans_data(directory, run_id)
    plans = [dict(row) for row in previous.get("plans", []) if isinstance(row, dict)]
    if not plans:
        raise ValueError("Claude Code selection requires at least one Planning candidate.")
    selected_plan_id, rationale = _select_with_claude(directory, plans, config, log)
    if selected_plan_id not in {str(row.get("plan_id") or "") for row in plans}:
        raise ValueError(f"Claude Code selected an unknown plan_id: {selected_plan_id}")
    return _apply_selection(directory, run_id, selected_plan_id, "claude_code", rationale, log)


def finish_plan_at_directory(directory: Path, run_id: str, plan_id: str, *, log: LogFn = print) -> dict[str, Any]:
    _require_public_entrypoint()
    return _apply_selection(directory, run_id, plan_id, "human", "Human selected this plan in the Web Plan controls.", log)


def update_plan_markdown_at_directory(directory: Path, run_id: str, markdown: str, *, log: LogFn = print) -> dict[str, Any]:
    _require_public_entrypoint()
    previous = _load_plans_data(directory, run_id)
    expected = [dict(row) for row in previous.get("plans", []) if isinstance(row, dict)]
    normalized = str(markdown or "").rstrip() + "\n"
    data = _projection_from_markdown(normalized, run_id, expected, previous=previous, source="human_edit")
    ideas_data = _load_ideas_data(directory, run_id)
    relevant_ids = {str(row.get("idea_id") or "") for row in data.get("plans", []) if isinstance(row, dict)}
    data["idea_revision"] = _ideas_revision([dict(row) for row in ideas_data.get("ideas", []) if isinstance(row, dict) and _idea_key(row) in relevant_ids])
    write_text(directory / PUBLIC_FINAL_PLAN_ARTIFACT, normalized)
    _write_plan_outputs(directory, data, normalized)
    update_manifest(directory, "plan")
    log("Human-edited canonical plan.md validated and projected.")
    return data
