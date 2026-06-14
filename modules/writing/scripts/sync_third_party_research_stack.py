#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths


THIRD_PARTY = ROOT / "third_party"
VENDOR_ROOT = ROOT / "modules" / "writing" / "vendor"
SKILL_ROOT = ROOT / ".claude" / "skills"
PROVENANCE_ROOT = ROOT / "runtime" / "method_references"

# These source repositories are method references, not runtime dependencies.
# Missing optional snapshots should leave an auditable warning, not disable TASTE
# native trajectory capabilities when local native contracts are present.
OPTIONAL_SOURCE_NAMES = {"ARIS", "EvoScientist", "academic-research-skills", "PaperOrchestra"}

ARIS_SKILLS = [
    "research-pipeline",
    "experiment-plan",
    "run-experiment",
    "experiment-audit",
    "auto-review-loop",
    "research-review",
    "paper-claim-audit",
    "citation-audit",
    "research-wiki",
    "experiment-queue",
    "kill-argument",
    "novelty-check",
    "paper-writing",
    "paper-figure",
    "paper-compile",
    "research-lit",
    "openalex",
    "semantic-scholar",
    "analyze-results",
    "result-to-claim",
    "research-refine",
    "meta-optimize",
    "auto-paper-improvement-loop",
]

EVO_SUBAGENTS = ["planner.yaml", "research.yaml", "code.yaml", "debug.yaml", "data_analysis.yaml", "writing.yaml"]
EVO_MIDDLEWARE = [
    "memory.py",
    "tool_error_handler.py",
    "context_overflow.py",
    "async_watcher.py",
    "tool_selector.py",
    "model_fallback.py",
]

ARS_SKILLS = ["academic-pipeline", "academic-paper", "academic-paper-reviewer", "deep-research"]
ARS_SHARED = [
    "cross_model_verification.md",
    "ground_truth_isolation_pattern.md",
    "artifact_reproducibility_pattern.md",
    "compliance_checkpoint_protocol.md",
    "handoff_schemas.md",
    "prisma_trAIce_protocol.md",
]
ARS_SCRIPTS = [
    "claim_audit_pipeline.py",
    "uncited_assertion_detector.py",
    "check_pipeline_integrity.py",
    "check_sprint_contract.py",
    "check_v3_9_2_phase_boundary.py",
    "check_v3_9_0_triangulation.py",
    "check_repro_lock.py",
]

PAPER_ORCHESTRA_SKILLS = [
    "agent-research-aggregator",
    "content-refinement-agent",
    "literature-review-agent",
    "outline-agent",
    "paper-autoraters",
    "paper-orchestra",
    "paper-writing-bench",
    "plotting-agent",
    "section-writing-agent",
]
LEGACY_EXTERNAL_SKILL_PREFIXES = ("aris-", "evoscientist-", "ars-", "paperorchestra-", "method-source-")
LEGACY_EXTERNAL_SKILL_NAMES = set(PAPER_ORCHESTRA_SKILLS)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_text(path: Path, limit: int | None = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit] if limit else text


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit(path: Path) -> str:
    if not path.exists():
        return ""
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_remote(path: Path) -> str:
    if not path.exists():
        return ""
    proc = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], cwd=ROOT, text=True, capture_output=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def first_frontmatter_value(text: str, key: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    return re.sub(r"-+", "-", value).strip("-")


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def as_path(value: Any) -> Path | None:
    if not value:
        return None
    raw = Path(str(value))
    return raw if raw.is_absolute() else ROOT / raw


def first_existing_path(candidates: list[Path]) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def source_root(source: dict[str, Any], *fallbacks: Path) -> Path:
    candidates: list[Path] = []
    for key in ("resolved_path", "local_path"):
        candidate = as_path(source.get(key))
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    for fallback in fallbacks:
        if fallback not in candidates:
            candidates.append(fallback)
    if not candidates:
        return ROOT / "missing-source"
    return first_existing_path(candidates)


def module_record(source: dict[str, Any], *, name: str, kind: str, path: Path) -> dict[str, Any]:
    source_available = bool(source.get("available"))
    available = source_available and path.exists()
    return {
        "name": name,
        "kind": kind,
        "path": relative(path),
        "available": available,
        "source_available": source_available,
        "source_family": source.get("name", ""),
        "optional_source_missing": not source_available and bool(source.get("optional", True)),
        "sha256": sha256(path),
    }


def write_skill_adapter(
    *,
    slug_name: str,
    title: str,
    description: str,
    source: dict[str, Any],
    source_files: list[Path],
    operating_contract: list[str],
    source_excerpt: str = "",
) -> dict[str, Any]:
    out_dir = PROVENANCE_ROOT / slug_name
    out_file = out_dir / "SKILL.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = [relative(path) for path in source_files if path.exists()]
    excerpt = source_excerpt.strip()
    if excerpt:
        excerpt = excerpt[:14000]
    body = [
        "---\n",
        f"name: {slug_name}\n",
        f"description: {description}\n",
        "---\n\n",
        f"# {title}\n\n",
        "This audit adapter is generated by `modules/writing/scripts/sync_third_party_research_stack.py` under ignored runtime storage so TASTE can summarize optional method references while operational prompts use native capability names.\n\n",
        "Do not present this source as an active agent, role, or separately invoked system. Runtime instructions must use native names such as research-direction management, evolutionary memory, evidence assurance, trajectory optimization, and paper production.\n\n",
        "## Audit Provenance\n",
        f"- Source family: {source.get('name', '')}\n",
        f"- Repository: {source.get('repository', '')}\n",
        f"- Local path: `{source.get('local_path', '')}`\n",
        f"- Commit: `{source.get('commit', '')}`\n",
        f"- License: {source.get('license', '')}\n",
        f"- License file: `{source.get('license_path', '')}`\n",
        "\n## Source Files\n",
    ]
    body.extend(f"- `{item}`\n" for item in evidence)
    body.extend([
        "\n## Operating Contract\n",
        "- Before acting, read the source files above if the task touches this capability.\n",
        "- Treat this adapter as provenance only: absorb the useful method into native modules, and keep evidence gates stricter than the source if they conflict.\n",
        "- Do not use source project names as role names, progress labels, prompt instructions, or paper prose.\n",
    ])
    body.extend(f"- {item}\n" for item in operating_contract)
    if source_excerpt:
        body.extend(["\n## Source Excerpt\n\n", "```markdown\n", excerpt, "\n```\n"])
    out_file.write_text("".join(body), encoding="utf-8")
    return {
        "name": slug_name,
        "title": title,
        "description": description,
        "path": relative(out_file),
        "source_family": source.get("name", ""),
        "source_files": evidence,
        "sha256": sha256(out_file),
    }


def cleanup_legacy_external_skill_dirs() -> list[str]:
    """Remove generated/source skill directories that would expose source projects as active Claude skills."""
    removed: list[str] = []
    if not SKILL_ROOT.exists():
        return removed
    for path in sorted(SKILL_ROOT.iterdir()):
        if not path.is_dir():
            continue
        name = path.name
        if name in {"experiment-loop", "evidence-gate", "writing"}:
            continue
        if name.startswith(LEGACY_EXTERNAL_SKILL_PREFIXES) or name in LEGACY_EXTERNAL_SKILL_NAMES:
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
            removed.append(relative(path))
    return removed


def source_record(name: str, paths: Path | list[Path], repository: str, license_name: str, *, optional: bool = True) -> dict[str, Any]:
    candidates = paths if isinstance(paths, list) else [paths]
    path = first_existing_path(candidates)
    license_path = path / "LICENSE"
    return {
        "name": name,
        "repository": git_remote(path) or repository,
        "local_path": relative(path),
        "resolved_path": relative(path),
        "candidate_paths": [relative(candidate) for candidate in candidates],
        "commit": git_commit(path),
        "license": license_name,
        "license_path": relative(license_path),
        "available": path.exists(),
        "optional": optional,
        "license_available": license_path.exists(),
    }


def aris_contract(skill_name: str) -> list[str]:
    return [
        f"Absorb the source `{skill_name}` protocol into the matching native phase.",
        "Convert every recommendation into an queue item, evidence reference, review verdict, or prune decision.",
        "Never let source review or writing instructions bypass `research_assurance_layer` or `research_evidence_manifest`.",
    ]


def sync_aris(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = source_root(source, THIRD_PARTY / "ARIS") / "skills"
    modules: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    for name in ARIS_SKILLS:
        path = root / name / "SKILL.md"
        text = read_text(path)
        desc = first_frontmatter_value(text, "description") or f"Source {name} research workflow contract."
        modules.append(module_record(source, name=name, kind="skill", path=path))
        if path.exists():
            skills.append(write_skill_adapter(
                slug_name=f"method-source-{slug(name)}",
                title=f"TASTE Method Provenance: {name}",
                description=f"Audit-only source method record for research trajectory work.",
                source=source,
                source_files=[path],
                operating_contract=aris_contract(name),
                source_excerpt=text,
            ))
    return modules, skills


def sync_evoscientist(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = source_root(source, THIRD_PARTY / "EvoScientist") / "EvoScientist"
    files = [root / "subagents" / item for item in EVO_SUBAGENTS] + [root / "middleware" / item for item in EVO_MIDDLEWARE]
    modules = [
        module_record(source, name=path.name, kind="subagent" if "subagents" in path.parts else "middleware", path=path)
        for path in files
    ]
    existing_files = [path for path in files if path.exists()]
    if not existing_files:
        return modules, []
    excerpts = [f"## {relative(path)}\n\n{read_text(path, 2600)}" for path in existing_files]
    skill = write_skill_adapter(
        slug_name="method-source-trajectory-system",
        title="TASTE Method Provenance: trajectory system",
        description="Audit-only source method record for multi-step TASTE trajectory optimization.",
        source=source,
        source_files=existing_files,
        operating_contract=[
            "Map planner/research/code/debug/data-analysis/writing stages onto native roles instead of running a one-shot agent.",
            "Use memory, tool-error, context-overflow, async-watcher, tool-selector, and model-fallback middleware concepts when designing retries and long-running Claude Code calls.",
            "Persist every retry/prune/repair outcome into TASTE evolutionary memory files; never keep decisions only in context.",
        ],
        source_excerpt="\n\n".join(excerpts),
    )
    return modules, [skill]


def sync_ars(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repo = source_root(source, VENDOR_ROOT / "academic-research-skills", THIRD_PARTY / "academic-research-skills")
    skill_files = [repo / name / "SKILL.md" for name in ARS_SKILLS]
    shared_files = [repo / "shared" / name for name in ARS_SHARED]
    script_files = [repo / "scripts" / name for name in ARS_SCRIPTS]
    modules = []
    for path in skill_files:
        modules.append(module_record(source, name=path.parent.name, kind="skill", path=path))
    for path in shared_files:
        modules.append(module_record(source, name=path.name, kind="shared_protocol", path=path))
    for path in script_files:
        modules.append(module_record(source, name=path.name, kind="audit_script", path=path))
    skills: list[dict[str, Any]] = []
    for path in skill_files:
        if not path.exists():
            continue
        name = path.parent.name
        text = read_text(path)
        skills.append(write_skill_adapter(
            slug_name=f"method-source-academic-{slug(name)}",
            title=f"TASTE Method Provenance: academic {name}",
            description=f"Audit-only source method record for citation, compliance, review, and paper quality safeguards.",
            source=source,
            source_files=[path, *shared_files, *script_files],
            operating_contract=[
                "Use the source contracts for citation provenance, phase-boundary discipline, claim audit, and non-leakage checks.",
                "Because the source is CC BY-NC 4.0, keep attribution and do not silently relicense or commercialize copied material.",
                "Run or mirror source audit scripts only as evidence checks; failed checks must become blockers or queue items.",
            ],
            source_excerpt=text,
        ))
    return modules, skills


def sync_paper_orchestra(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repo = source_root(source, VENDOR_ROOT / "PaperOrchestra", THIRD_PARTY / "PaperOrchestra")
    source_files = [repo / "skills" / name / "SKILL.md" for name in PAPER_ORCHESTRA_SKILLS]
    modules = [
        module_record(source, name=path.parent.name, kind="skill", path=path)
        for path in source_files
    ]
    existing_files = [path for path in source_files if path.exists()]
    if not existing_files:
        return modules, []
    # The source skills are mirrored by the paper bridge; this adapter binds their useful pattern to the native paper-production layer.
    skill = write_skill_adapter(
        slug_name="method-source-paper-production",
        title="TASTE Method Provenance: paper production",
        description="Audit-only source method record for TASTE evidence-bound paper production and venue gates.",
        source=source,
        source_files=existing_files,
        operating_contract=[
            "Use section/outline/literature/plotting/review stages for paper generation and revision.",
            "Keep the workflow as the final gatekeeper: venue template, citation provenance, figure quality, and evidence readiness must pass before showing a paper as accepted preview.",
            "If paper output is weak, run preview/figure/citation repair loops rather than hand-editing scientific claims.",
        ],
        source_excerpt="\n\n".join(f"## {relative(path)}\n\n{read_text(path, 1800)}" for path in existing_files),
    )
    return modules, [skill]


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "third_party_research_stack.md"
    lines = ["# Third-Party Research Stack\n\n"]
    lines.append(f"- updated_at: {payload.get('updated_at', '')}\n")
    lines.append(f"- status: {payload.get('status', '')}\n")
    lines.append(f"- source_count: {payload.get('summary', {}).get('source_count', 0)}\n")
    lines.append(f"- available_source_count: {payload.get('summary', {}).get('available_source_count', 0)}\n")
    lines.append(f"- optional_missing_source_count: {payload.get('summary', {}).get('optional_missing_source_count', 0)}\n")
    lines.append(f"- selected_module_count: {payload.get('summary', {}).get('selected_module_count', 0)}\n")
    lines.append(f"- available_module_count: {payload.get('summary', {}).get('available_module_count', 0)}\n")
    lines.append(f"- synced_skill_count: {payload.get('summary', {}).get('synced_skill_count', 0)}\n")
    lines.append(f"- missing_module_count: {payload.get('summary', {}).get('missing_module_count', 0)}\n")
    lines.append(f"- optional_missing_module_count: {payload.get('summary', {}).get('optional_missing_module_count', 0)}\n\n")
    for warning in payload.get("warnings", []):
        lines.append(f"- warning: {warning}\n")
    if payload.get("warnings"):
        lines.append("\n")
    lines.append("## Sources\n")
    for source in payload.get("sources", []):
        lines.append(f"- {source.get('name')}: {source.get('repository')} @ `{source.get('commit')}` | license={source.get('license')} | path=`{source.get('local_path')}`\n")
    lines.append("\n## Synced Skill Adapters\n")
    for skill in payload.get("synced_skill_adapters", []):
        lines.append(f"- {skill.get('name')}: `{skill.get('path')}` | source={skill.get('source_family')}\n")
    lines.append("\n## Selected External Modules\n")
    for family in payload.get("families", []):
        lines.append(f"\n### {family.get('name')}\n\n")
        for module in family.get("modules", []):
            flag = "ok" if module.get("available") else "missing"
            lines.append(f"- [{flag}] {module.get('kind')} `{module.get('name')}` | `{module.get('path')}`\n")
    lines.append("\n## License Notes\n")
    for note in payload.get("license_notes", []):
        lines.append(f"- {note}\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines), encoding="utf-8")
    return out


def build_stack(project: str) -> dict[str, Any]:
    paths = build_paths(project)
    removed_legacy_skill_dirs = cleanup_legacy_external_skill_dirs()
    sources = [
        source_record("ARIS", THIRD_PARTY / "ARIS", "https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep.git", "MIT"),
        source_record("EvoScientist", THIRD_PARTY / "EvoScientist", "https://github.com/EvoScientist/EvoScientist.git", "Apache-2.0"),
        source_record("academic-research-skills", [VENDOR_ROOT / "academic-research-skills", THIRD_PARTY / "academic-research-skills"], "https://github.com/Imbad0202/academic-research-skills.git", "CC BY-NC 4.0"),
        source_record("PaperOrchestra", [VENDOR_ROOT / "PaperOrchestra", THIRD_PARTY / "PaperOrchestra"], "https://github.com/Ar9av/PaperOrchestra.git", "third-party repository license; see source tree"),
    ]
    source_by_name = {source["name"]: source for source in sources}
    families = []
    synced_skills: list[dict[str, Any]] = []
    for name, syncer in [
        ("ARIS", sync_aris),
        ("EvoScientist", sync_evoscientist),
        ("academic-research-skills", sync_ars),
        ("PaperOrchestra", sync_paper_orchestra),
    ]:
        modules, skills = syncer(source_by_name[name])
        families.append({"name": name, "modules": modules})
        synced_skills.extend(skills)
    modules = [module for family in families for module in family.get("modules", [])]
    available_modules = [module for module in modules if module.get("available")]
    missing_available_modules = [module for module in modules if module.get("source_available") and not module.get("available")]
    optional_missing_modules = [module for module in modules if not module.get("source_available")]
    missing_sources = [source for source in sources if not source.get("available")]
    available_sources = [source for source in sources if source.get("available")]
    warnings: list[str] = []
    if missing_sources:
        warnings.append("optional method reference sources missing: " + ", ".join(source.get("name", "") for source in missing_sources))
    if missing_available_modules:
        warnings.append("available method reference snapshots are incomplete: " + ", ".join(module.get("path", "") for module in missing_available_modules[:10]))
    status = "ready" if synced_skills and not missing_available_modules else "blocked"
    payload = {
        "project": project,
        "updated_at": now_iso(),
        "status": status,
        "sources": sources,
        "families": families,
        "synced_skill_adapters": synced_skills,
        "removed_legacy_skill_dirs": removed_legacy_skill_dirs,
        "warnings": warnings,
        "capability_bindings": [
            {"capability": "research_direction_management", "native_contract": "Maintain landscape, novelty map, failed-hypothesis graph, and unexplored-niche graph with evidence-backed search pressure.", "source_method_refs": ["source:research-pipeline", "source:novelty-check", "source:deep-research"]},
            {"capability": "evolutionary_memory", "native_contract": "Persist ideation, experimentation, retry, prune, repair, and recoverable-exception memory to disk across trajectory turns.", "source_method_refs": ["source:memory", "source:staged-research-roles"]},
            {"capability": "research_assurance_layer", "native_contract": "Reject unsupported claims, audit citations, require local evidence paths, and convert failed checks into blockers or queue items.", "source_method_refs": ["source:claim-audit", "source:citation-audit", "source:uncited-assertion-detector"]},
            {"capability": "trajectory_system", "native_contract": "Run long-horizon plan -> execute -> evaluate -> repair/prune loops with recoverable errors, async progress tracking, and bounded retries.", "source_method_refs": ["source:tool-error-handler", "source:context-overflow", "source:async-watcher", "source:experiment-queue"]},
            {"capability": "paper_production", "native_contract": "Build papers section-by-section with outline, literature, figure/table, reviewer, citation, venue-template, and readiness gates tied to TASTE evidence.", "source_method_refs": ["source:section-writing", "source:literature", "source:plotting", "source:review", "source:academic-paper"]},
        ],
        "license_notes": [
            "Source provenance, commit, and license paths are retained for audit only; operational Workflow prompts must use native module names.",
            "Source excerpts remain local audit material and must not be presented as separately invoked project agents.",
            "CC BY-NC 4.0 source material keeps attribution and should remain local/non-commercial unless relicensing is resolved.",
            "TASTE stores provenance and keeps evidence/venue gates as final authority over any imported method pattern.",
        ],
        "summary": {
            "source_count": len(sources),
            "available_source_count": len(available_sources),
            "optional_missing_source_count": len(missing_sources),
            "available_source_names": [source.get("name", "") for source in available_sources],
            "optional_missing_source_names": [source.get("name", "") for source in missing_sources],
            "selected_module_count": len(modules),
            "available_module_count": len(available_modules),
            "missing_module_count": len(missing_available_modules),
            "optional_missing_module_count": len(optional_missing_modules),
            "synced_skill_count": len(synced_skills),
            "license_count": len([source for source in available_sources if source.get("license_available")]),
            "removed_legacy_skill_dir_count": len(removed_legacy_skill_dirs),
        },
    }
    report = write_report(paths, payload)
    payload["report"] = relative(report)
    payload["state_path"] = relative(paths.state / "third_party_research_stack.json")
    save_json(paths.state / "third_party_research_stack.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize source-method references into native trajectory state.")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    payload = build_stack(args.project)
    print(json.dumps({
        "project": args.project,
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "state": payload.get("state_path"),
        "report": payload.get("report"),
    }, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
