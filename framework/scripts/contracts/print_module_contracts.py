#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from taste_backend.orchestration.orchestrator import main

if __name__ == "__main__":
    raise SystemExit(main(["contracts", *sys.argv[1:]]))
