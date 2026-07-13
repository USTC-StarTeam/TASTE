from __future__ import annotations

import os
import sys
from pathlib import Path

ENTRYPOINT_ENV = "EXPERIMENTING_PUBLIC_ENTRYPOINT_ACTIVE"


def _in_taste_env() -> bool:
    return os.environ.get("CONDA_DEFAULT_ENV") == "taste" or Path(sys.prefix).name == "taste"


def ensure_main_entrypoint() -> None:
    if os.environ.get(ENTRYPOINT_ENV) != "1":
        raise SystemExit(
            "Experimenting private scripts are not public entrypoints. "
            "Use: conda run -n taste python modules/experimenting/main.py --action <action> ..."
        )
    if not _in_taste_env():
        raise SystemExit(
            "Experimenting must run under the conda environment named 'taste'. "
            "Use: conda run -n taste python modules/experimenting/main.py --action <action> ..."
        )
