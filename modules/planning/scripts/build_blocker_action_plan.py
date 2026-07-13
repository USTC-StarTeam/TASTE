#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

if os.environ.get("PLANNING_PUBLIC_ENTRYPOINT_ACTIVE") != "1":
    raise SystemExit("Use modules/planning/main.py to call Planning functionality.")

exec(compile((Path(__file__).resolve().parent / "blockers" / "build_blocker_action_plan.py").read_text(encoding="utf-8"), str(Path(__file__).resolve().parent / "blockers" / "build_blocker_action_plan.py"), "exec"), globals())
