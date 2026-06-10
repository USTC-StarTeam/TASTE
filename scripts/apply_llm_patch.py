#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def safe_repo_path(repo: Path, rel_path: str) -> Path:
    candidate = Path(rel_path)
    if candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError(f'unsafe patch path outside repo: {rel_path}')
    resolved = (repo / candidate).resolve()
    repo_resolved = repo.resolve()
    if resolved != repo_resolved and repo_resolved not in resolved.parents:
        raise ValueError(f'unsafe patch path outside repo: {rel_path}')
    return resolved

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def replace_once(text: str, old: str, new: str, cursor: int) -> tuple[str, int]:
    candidates = [(old, new)]
    if old and not old.endswith("\n"):
        candidates.append((old + "\n", new + "\n"))
    for source, target in candidates:
        idx = text.find(source, cursor)
        if idx != -1:
            updated = text[:idx] + target + text[idx + len(source):]
            return updated, idx + len(target)
    for source, target in candidates:
        idx = text.find(source)
        if idx != -1:
            updated = text[:idx] + target + text[idx + len(source):]
            return updated, idx + len(target)
    raise ValueError("hunk context not found")


def parse_apply_patch(text: str) -> list[dict]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != BEGIN_PATCH:
        raise ValueError("invalid apply_patch header")
    if END_PATCH not in lines:
        raise ValueError("truncated apply_patch: missing *** End Patch")
    ops: list[dict] = []
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line == END_PATCH:
            break
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            idx += 1
            content: list[str] = []
            while idx < len(lines) and not lines[idx].startswith("*** "):
                if not lines[idx].startswith("+"):
                    raise ValueError(f"invalid add-file line: {lines[idx]}")
                content.append(lines[idx][1:])
                idx += 1
            ops.append({"op": "add", "path": path, "content": "\n".join(content) + ("\n" if content else "")})
            continue
        if line.startswith("*** Delete File: "):
            ops.append({"op": "delete", "path": line[len("*** Delete File: "):].strip()})
            idx += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            idx += 1
            move_to = ""
            if idx < len(lines) and lines[idx].startswith("*** Move to: "):
                move_to = lines[idx][len("*** Move to: "):].strip()
                idx += 1
            blocks: list[list[str]] = []
            current: list[str] = []
            while idx < len(lines):
                inner = lines[idx]
                if inner == END_PATCH or inner.startswith("*** Add File: ") or inner.startswith("*** Delete File: ") or inner.startswith("*** Update File: "):
                    break
                if inner.startswith("@@"):
                    if current:
                        blocks.append(current)
                        current = []
                elif inner == "*** End of File":
                    pass
                else:
                    current.append(inner)
                idx += 1
            if current:
                blocks.append(current)
            ops.append({"op": "update", "path": path, "move_to": move_to, "blocks": blocks})
            continue
        raise ValueError(f"unsupported apply_patch directive: {line}")
    return ops


def apply_update(repo: Path, op: dict) -> None:
    path = safe_repo_path(repo, op["path"])
    if not path.exists():
        raise FileNotFoundError(op["path"])
    text = path.read_text(encoding="utf-8")
    cursor = 0
    for block in op.get("blocks", []):
        old_lines: list[str] = []
        new_lines: list[str] = []
        for line in block:
            if not line:
                old_lines.append("")
                new_lines.append("")
                continue
            prefix = line[0]
            body = line[1:]
            if prefix == " ":
                old_lines.append(body)
                new_lines.append(body)
            elif prefix == "-":
                old_lines.append(body)
            elif prefix == "+":
                new_lines.append(body)
            else:
                raise ValueError(f"invalid update line: {line}")
        old = "\n".join(old_lines)
        new = "\n".join(new_lines)
        text, cursor = replace_once(text, old, new, cursor)
    destination = safe_repo_path(repo, (op.get("move_to") or op["path"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    if op.get("move_to") and destination != path:
        path.unlink()


def apply_custom_patch(repo: Path, patch_text: str) -> dict:
    ops = parse_apply_patch(patch_text)
    for op in ops:
        if op["op"] == "add":
            target = safe_repo_path(repo, op["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op["content"], encoding="utf-8")
        elif op["op"] == "delete":
            target = safe_repo_path(repo, op["path"])
            if target.exists():
                target.unlink()
        elif op["op"] == "update":
            apply_update(repo, op)
        else:
            raise ValueError(f"unsupported op: {op[op]}")
    return {"applied": True, "patch_mode": "custom-apply_patch", "operation_count": len(ops)}


def apply_unified_diff(repo: Path, patch_text: str) -> dict:
    patch_bin = shutil.which("patch")
    if not patch_bin:
        raise RuntimeError("patch-not-found")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(patch_text)
        temp_patch = Path(handle.name)
    dry = run([patch_bin, "-p0", "--dry-run", "-i", str(temp_patch)], repo)
    if dry.returncode != 0:
        dry = run([patch_bin, "-p1", "--dry-run", "-i", str(temp_patch)], repo)
        level = "-p1"
    else:
        level = "-p0"
    if dry.returncode != 0:
        raise RuntimeError(json.dumps({"reason": "dry-run-failed", "stdout": dry.stdout[-2000:], "stderr": dry.stderr[-2000:]}, ensure_ascii=False))
    try:
        apply = run([patch_bin, level, "-i", str(temp_patch)], repo)
        if apply.returncode != 0:
            raise RuntimeError(json.dumps({"reason": "apply-failed", "stdout": apply.stdout[-2000:], "stderr": apply.stderr[-2000:]}, ensure_ascii=False))
        return {"applied": True, "patch_mode": "unified-diff", "patch_level": level, "stdout": apply.stdout[-2000:], "stderr": apply.stderr[-2000:]}
    finally:
        try:
            temp_patch.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--patch-path", required=True)
    args = parser.parse_args()

    repo = Path(args.repo_path).resolve()
    patch_path = Path(args.patch_path).resolve()
    if not repo.exists():
        raise SystemExit(f"missing repo path: {repo}")
    if not patch_path.exists():
        raise SystemExit(f"missing patch path: {patch_path}")

    patch_text = patch_path.read_text(encoding="utf-8", errors="ignore").strip()
    if patch_text and not patch_text.endswith("\n"):
        patch_text += "\n"
    if not patch_text:
        print(json.dumps({"applied": False, "reason": "empty_patch"}, ensure_ascii=False))
        raise SystemExit(2)

    try:
        if patch_text.startswith(BEGIN_PATCH):
            result = apply_custom_patch(repo, patch_text)
        else:
            result = apply_unified_diff(repo, patch_text)
    except Exception as exc:
        print(json.dumps({"applied": False, "reason": "patch-apply-failed", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
