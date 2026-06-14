from __future__ import annotations

from path_helpers import ensure_script_paths


def pytest_configure(config):
    ensure_script_paths()
