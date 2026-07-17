from __future__ import annotations

import sys


def module_command(stage: str, action: str, *args: str) -> list[str]:
    return [sys.executable, "framework/scripts/main.py", "module", stage, "--action", action, *args]
