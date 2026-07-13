from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from scripts.common.io_utils import ensure_within, read_text_limited, short_hash, slugify
from scripts.common.shell import run_logged

README_NAMES = ("README.md", "README.rst", "README.txt", "readme.md", "Readme.md")
CONFIG_PATTERNS = ("environment*.yml", "environment*.yaml", "conda*.yml", "conda*.yaml", "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg")
DOC_EXTENSIONS = {".md", ".rst", ".txt"}
DOC_KEYWORDS = ("install", "setup", "usage", "quickstart", "reproduce", "reproduction", "train", "training", "eval", "evaluation", "data", "dataset", "benchmark", "run")
ENTRYPOINT_KEYWORDS = ("train", "eval", "evaluate", "test", "run", "main", "infer", "inference", "finetune", "pretrain", "download", "prepare", "preprocess")
SKIP_DIR_NAMES = {".git", ".github", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", "data", "datasets", "outputs", "output", "runs", "logs", "checkpoints", "weights"}


def repo_slug(repo_url: str) -> str:
    cleaned = re.sub(r"\.git$", "", str(repo_url or "").rstrip("/"))
    name = cleaned.rsplit("/", 1)[-1] if cleaned else "repo"
    owner = cleaned.rstrip("/").split("/")[-2] if "/" in cleaned else "github"
    return slugify(f"{owner}_{name}_{short_hash(repo_url, 8)}", "repo")


def _git_head(repo_path: Path) -> str:
    if not (repo_path / ".git").exists():
        return ""
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, text=True, capture_output=True, timeout=30)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def _canonical_repo_url(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text[len("git@github.com:"):]
    elif text.startswith("ssh://git@github.com/"):
        text = "https://github.com/" + text[len("ssh://git@github.com/"):]
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/").lower()


def _repo_origin_urls(repo_path: Path) -> list[str]:
    urls: list[str] = []
    if not (repo_path / ".git").exists():
        return urls
    try:
        proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path, text=True, capture_output=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            urls.append(proc.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    config_path = repo_path / ".git" / "config"
    if config_path.exists():
        try:
            config_text = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            config_text = ""
        for match in re.finditer(r"^\s*url\s*=\s*(.+)$", config_text, flags=re.M):
            urls.append(match.group(1).strip())
    return urls


def _repo_origin_matches(repo_url: str, repo_path: Path) -> bool:
    target = _canonical_repo_url(repo_url)
    if not target:
        return False
    return target in {_canonical_repo_url(url) for url in _repo_origin_urls(repo_path)}


def _looks_like_taste_workspace_copy(repo_path: Path) -> bool:
    if not (repo_path / "工作状态.txt").exists():
        return False
    workspace_markers = [
        (repo_path / "modules").is_dir(),
        (repo_path / "web").is_dir(),
        (repo_path / "framework").is_dir(),
        (repo_path / "projects").is_dir(),
        (repo_path / "taste_web.log").exists(),
        (repo_path / "CLAUDE.md").exists(),
    ]
    return sum(1 for item in workspace_markers if item) >= 1


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _invalid_repo_reason(repo_url: str, repo_path: Path) -> str:
    if not repo_path.exists():
        return "missing"
    if not (repo_path / ".git").exists():
        return "not_a_git_repository"
    if _looks_like_taste_workspace_copy(repo_path):
        return "taste_workspace_copy"
    if not _repo_origin_matches(repo_url, repo_path):
        origins = ", ".join(_repo_origin_urls(repo_path)) or "<no origin>"
        return f"origin_mismatch: expected {_canonical_repo_url(repo_url)}, got {origins}"
    return ""


def _failed_validation_receipt(repo_url: str, target: Path, log_path: Path, reason: str, previous_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    receipt = {
        "command": f"validate cloned repo {target}",
        "tokens": ["validate_cloned_repo", str(target)],
        "cwd": str(target.parent),
        "log_path": str(log_path),
        "required": True,
        "status": "failed",
        "return_code": 128,
        "stdout_tail": "",
        "stderr_tail": f"Invalid cloned repository for {repo_url}: {reason}",
        "invalid_clone_reason": reason,
    }
    if previous_receipt:
        receipt["previous_clone_receipt"] = previous_receipt
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[仓库校验失败] {repo_url}\n")
        handle.write(f"target={target}\n")
        handle.write(f"reason={reason}\n")
    return receipt


def _repair_invalid_clone(repo_url: str, target: Path, log_path: Path, previous_receipt: dict[str, Any]) -> tuple[dict[str, Any], str]:
    reason = _invalid_repo_reason(repo_url, target)
    if not reason or reason == "missing":
        return previous_receipt, reason
    _remove_path(target)
    return _failed_validation_receipt(repo_url, target, log_path, reason, previous_receipt), reason


def clone_or_reuse(repo_url: str, repos_dir: Path, log_dir: Path, branch: str = "", commit: str = "", timeout_sec: int = 900, env: dict[str, str] | None = None) -> dict[str, Any]:
    run_dir = repos_dir.expanduser().resolve().parent
    try:
        repos_dir = ensure_within(repos_dir, run_dir)
        log_dir = ensure_within(log_dir, run_dir)
        target = ensure_within(repos_dir / repo_slug(repo_url), repos_dir)
    except Exception as exc:
        return {
            "repo_url": repo_url,
            "repo_path": "",
            "exists": False,
            "clone_receipt": {
                "status": "blocked_by_path_guard",
                "return_code": 126,
                "stderr_tail": f"仓库目录路径守卫拒绝：{type(exc).__name__}: {exc}",
            },
            "checkout_receipt": {},
            "head_commit": "",
        }
    repos_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"git_clone_{target.name}.log"
    if target.exists() and _invalid_repo_reason(repo_url, target):
        _remove_path(target)
    receipt: dict[str, Any] = {}
    if not target.exists():
        cmd = ["git", "clone"]
        if not commit:
            cmd.extend(["--depth", "1"])
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([repo_url, str(target)])
        receipt = run_logged(cmd, cwd=repos_dir, log_path=log_path, timeout_sec=timeout_sec, env=env)
    else:
        receipt = run_logged(["git", "fetch", "--all", "--prune"], cwd=target, log_path=log_path, timeout_sec=timeout_sec, required=False, env=env)
    receipt, _invalid_before_checkout = _repair_invalid_clone(repo_url, target, log_path, receipt)
    checkout_receipt: dict[str, Any] = {}
    if commit and target.exists():
        checkout_receipt = run_logged(["git", "checkout", commit], cwd=target, log_path=log_path, timeout_sec=120, env=env)
    final_invalid = _invalid_repo_reason(repo_url, target)
    if final_invalid and final_invalid != "missing":
        _remove_path(target)
        receipt = _failed_validation_receipt(repo_url, target, log_path, final_invalid, receipt)
    head_text = _git_head(target) if target.exists() else ""
    head = {"stdout_tail": head_text} if head_text else {}
    return {
        "repo_url": repo_url,
        "repo_path": str(target),
        "clone_receipt": receipt,
        "checkout_receipt": checkout_receipt,
        "requested_branch_or_tag": branch,
        "requested_commit": commit,
        "head_commit": str(head.get("stdout_tail") or "").strip().splitlines()[-1] if head.get("stdout_tail") else "",
        "exists": target.exists(),
    }


def _skip_candidate(path: Path, repo: Path) -> bool:
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return True
    return any(part in SKIP_DIR_NAMES for part in rel.parts[:-1])


def _glob_limited(repo: Path, pattern: str, limit: int = 20, recursive: bool = False) -> list[str]:
    out: list[str] = []
    iterator = repo.rglob(pattern) if recursive else repo.glob(pattern)
    for path in sorted(iterator):
        if path.is_file() and not _skip_candidate(path, repo):
            out.append(str(path.relative_to(repo)))
        if len(out) >= limit:
            break
    return out


def _relative_parts_text(path: Path, repo: Path) -> str:
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return path.name.lower()
    return " ".join(part.lower() for part in rel.parts)


def _looks_like_helpful_doc(path: Path, repo: Path) -> bool:
    if path.suffix.lower() not in DOC_EXTENSIONS:
        return False
    name = path.name.lower()
    rel_text = _relative_parts_text(path, repo)
    if name.startswith("readme"):
        return True
    return any(keyword in rel_text for keyword in DOC_KEYWORDS)


def _collect_document_files(repo: Path, limit: int = 30) -> list[str]:
    out: list[str] = []
    search_roots = [repo / "docs", repo / "doc", repo / "examples", repo / "example", repo / "scripts"]
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if len(out) >= limit:
                return out
            if path.is_file() and not _skip_candidate(path, repo) and _looks_like_helpful_doc(path, repo):
                rel = str(path.relative_to(repo))
                if rel not in out:
                    out.append(rel)
    for path in sorted(repo.glob("*")):
        if len(out) >= limit:
            break
        if path.is_file() and _looks_like_helpful_doc(path, repo):
            rel = str(path.relative_to(repo))
            if rel not in out:
                out.append(rel)
    return out


def _collect_config_files(repo: Path, limit: int = 60) -> list[str]:
    out: list[str] = []
    for pattern in CONFIG_PATTERNS:
        for rel in _glob_limited(repo, pattern, limit=limit, recursive=True):
            if rel not in out:
                out.append(rel)
            if len(out) >= limit:
                return out
    for root_name in ["configs", "config", "conf"]:
        root = repo / root_name
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if len(out) >= limit:
                return out
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml", ".json", ".toml"} and not _skip_candidate(path, repo):
                rel = str(path.relative_to(repo))
                if rel not in out:
                    out.append(rel)
    return out


def _collect_python_entrypoints(repo: Path, limit: int = 120) -> list[str]:
    out: list[str] = []
    roots = [repo, repo / "scripts", repo / "examples", repo / "tools", repo / "src"]
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        iterator = root.glob("*.py") if root == repo else root.rglob("*.py")
        for path in sorted(iterator):
            if len(out) >= limit:
                return out
            if _skip_candidate(path, repo):
                continue
            stem = path.stem.lower()
            rel_text = _relative_parts_text(path, repo)
            if any(keyword in stem or keyword in rel_text for keyword in ENTRYPOINT_KEYWORDS):
                rel = str(path.relative_to(repo))
                if rel not in out:
                    out.append(rel)
    return out


COMMAND_LINE_HEAD_RE = re.compile(r"^(?:pip\s+install|pip3\s+install|conda\s+|mamba\s+|micromamba\s+|python\s+|python3\s+|torchrun\s+|accelerate\s+launch\s+|deepspeed\s+|bash\s+|sh\s+)", re.I)
ENV_ASSIGNMENT_RE = re.compile(r"^(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+")


def _normalize_command_line(raw: str) -> str:
    line = raw.strip().strip("`")
    line = re.sub(r"^[>$#]\s*", "", line)
    line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
    return line.strip()


def _command_match_text(line: str) -> str:
    return ENV_ASSIGNMENT_RE.sub("", line, count=1).strip()


def _logical_command_source_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            if buffer:
                lines.append(buffer.strip())
                buffer = ""
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            continue
        has_continuation = stripped.endswith("\\")
        current = stripped[:-1].rstrip() if has_continuation else stripped
        if buffer:
            buffer = f"{buffer} {current.lstrip()}"
        else:
            buffer = current
        if not has_continuation:
            lines.append(buffer.strip())
            buffer = ""
    if buffer:
        lines.append(buffer.strip())
    return lines


def _extract_command_lines(text: str) -> list[str]:
    commands: list[str] = []
    for raw in _logical_command_source_lines(text):
        line = _normalize_command_line(raw)
        if COMMAND_LINE_HEAD_RE.match(_command_match_text(line)):
            if 4 <= len(line) <= 500 and line not in commands:
                commands.append(line)
        if len(commands) >= 80:
            break
    return commands


def _text_evidence_row(repo: Path, rel: str, limit: int) -> dict[str, Any]:
    path = repo / rel
    text = read_text_limited(path, limit)
    return {"path": str(path), "relative_path": rel, "text_excerpt": text, "command_lines": _extract_command_lines(text)}


def _entrypoint_evidence_row(repo: Path, rel: str) -> dict[str, Any]:
    return _text_evidence_row(repo, rel, 8000)


def collect_repo_evidence(repo_path: Path) -> dict[str, Any]:
    repo = repo_path.resolve()
    readmes: list[dict[str, Any]] = []
    for name in README_NAMES:
        path = repo / name
        if path.exists() and path.is_file():
            readmes.append(_text_evidence_row(repo, name, 30000))
    if not readmes:
        for rel in _glob_limited(repo, "README*", limit=8):
            readmes.append(_text_evidence_row(repo, rel, 30000))
    documentation_files: list[dict[str, Any]] = []
    readme_rels = {row.get("relative_path") for row in readmes}
    for rel in _collect_document_files(repo, limit=30):
        if rel not in readme_rels:
            documentation_files.append(_text_evidence_row(repo, rel, 18000))
    config_files: list[dict[str, str]] = []
    for rel in _collect_config_files(repo, limit=60):
        path = repo / rel
        config_files.append({"path": str(path), "relative_path": rel, "text_excerpt": read_text_limited(path, 20000)})
    top_level_files = [str(path.relative_to(repo)) for path in sorted(repo.iterdir())[:120]] if repo.exists() else []
    python_entrypoints = [_entrypoint_evidence_row(repo, rel) for rel in _collect_python_entrypoints(repo, limit=120)]
    return {
        "schema_version": "environment.repo_evidence.v1",
        "repo_path": str(repo),
        "readmes": readmes,
        "documentation_files": documentation_files,
        "config_files": config_files,
        "top_level_files": top_level_files,
        "python_entrypoints": python_entrypoints,
        "evidence_summary": {
            "readme_count": len(readmes),
            "documentation_file_count": len(documentation_files),
            "config_file_count": len(config_files),
            "python_entrypoint_count": len(python_entrypoints),
        },
    }
