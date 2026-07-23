from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_ideation_main():
    spec = importlib.util.spec_from_file_location("ideation_main_environment_test", ROOT / "modules" / "ideation" / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ideation_accepts_taste_python_without_conda_marker(monkeypatch):
    ideation_main = _load_ideation_main()
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.setattr(ideation_main.sys, "executable", "/opt/miniconda3/envs/taste/bin/python3")

    ideation_main._require_taste_conda()

    monkeypatch.setattr(ideation_main.sys, "executable", "/usr/bin/python3")
    with pytest.raises(SystemExit, match="conda environment named 'taste'"):
        ideation_main._require_taste_conda()


def test_start_web_activates_management_python_conda_prefix(tmp_path):
    conda_base = tmp_path / "miniconda3"
    env_prefix = conda_base / "envs" / "taste"
    conda_exe = conda_base / "bin" / "conda"
    conda_sh = conda_base / "etc" / "profile.d" / "conda.sh"
    management_python = env_prefix / "bin" / "python3"
    captured_env = tmp_path / "captured.env"
    bashrc = tmp_path / ".bashrc"

    conda_exe.parent.mkdir(parents=True)
    conda_sh.parent.mkdir(parents=True)
    management_python.parent.mkdir(parents=True)
    conda_exe.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == info && \"$2\" == --base ]]; then\n"
        f"  printf '%s\\n' '{conda_base}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    conda_sh.write_text(
        "conda() {\n"
        "  if [[ \"$1\" == activate ]]; then\n"
        "    export CONDA_PREFIX=\"$2\"\n"
        "    export CONDA_DEFAULT_ENV=\"${2##*/}\"\n"
        "    export PATH=\"$2/bin:$PATH\"\n"
        "    return 0\n"
        "  fi\n"
        "  return 1\n"
        "}\n",
        encoding="utf-8",
    )
    management_python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == -c ]]; then\n"
        f"  printf '%s\\n' '{env_prefix}'\n"
        "  exit 0\n"
        "fi\n"
        f"printf 'CONDA_DEFAULT_ENV=%s\\nCONDA_PREFIX=%s\\nBASHRC_AFTER_GUARD=%s\\n' \"${{CONDA_DEFAULT_ENV:-}}\" \"${{CONDA_PREFIX:-}}\" \"${{BASHRC_AFTER_GUARD:-}}\" > '{captured_env}'\n",
        encoding="utf-8",
    )
    bashrc.write_text(
        "case $- in\n"
        "  *i*) ;;\n"
        "  *) return ;;\n"
        "esac\n"
        "export BASHRC_AFTER_GUARD=loaded\n",
        encoding="utf-8",
    )
    conda_exe.chmod(0o755)
    management_python.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "MANAGEMENT_PYTHON": str(management_python),
        "CONDA_EXE": str(conda_exe),
        "SOURCE_BASHRC": "1",
        "HOME": str(tmp_path),
    })
    env.pop("CONDA_DEFAULT_ENV", None)
    env.pop("CONDA_PREFIX", None)
    subprocess.run(
        [str(ROOT / "framework" / "scripts" / "launchers" / "start_web.sh")],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert captured_env.read_text(encoding="utf-8").splitlines() == [
        "CONDA_DEFAULT_ENV=taste",
        f"CONDA_PREFIX={env_prefix}",
        "BASHRC_AFTER_GUARD=loaded",
    ]
