#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

exec(compile((Path(__file__).resolve().parent / "blockers" / "build_blocker_action_plan.py").read_text(encoding="utf-8"), str(Path(__file__).resolve().parent / "blockers" / "build_blocker_action_plan.py"), "exec"), globals())
