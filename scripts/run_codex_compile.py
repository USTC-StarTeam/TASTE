#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, load_project_config

PROMPT = ROOT / "prompts" / "wiki_compiler.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    if not cfg.get("loop", {}).get("codex_enabled", True):
        print("codex-disabled")
        return 0

    codex = shutil.which("codex")
    if not codex:
        print("codex-not-found")
        return 1

    prompt_text = PROMPT.read_text(encoding="utf-8") + f"\n\nProject root: {paths.root}\n"
    cmd = [
        codex,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(paths.root),
        prompt_text,
    ]
    proc = subprocess.run(
        cmd,
        cwd=paths.root,
        text=True,
        capture_output=True,
        timeout=int(cfg.get("loop", {}).get("codex_timeout_sec", 1200)),
    )
    (paths.logs / "codex_compile.log").write_text(proc.stdout + "\n--- STDERR ---\n" + proc.stderr, encoding="utf-8")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
