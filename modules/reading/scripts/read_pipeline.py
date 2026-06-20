from __future__ import annotations

import pipeline.read_pipeline as _read_pipeline_impl
from pipeline.read_pipeline import *  # noqa: F401,F403
from pipeline.read_pipeline import (
    _arxiv_pdf_candidates,
    _download_first_readable_pdf,
    _download_pdf,
    _extract_pdf_text,
    _pdf_candidates_for_reading,
)


def _sync_compat_monkeypatches() -> None:
    for name in [
        "LLMClient",
        "requests",
        "_arxiv_pdf_candidates",
        "_download_first_readable_pdf",
        "_download_pdf",
        "_extract_pdf_text",
        "_pdf_candidates_for_reading",
    ]:
        if name in globals():
            setattr(_read_pipeline_impl, name, globals()[name])


def run_read(*args, **kwargs):
    _sync_compat_monkeypatches()
    return _read_pipeline_impl.run_read(*args, **kwargs)


if __name__ == "__main__":
    raise SystemExit("请通过 modules/reading/main.py 调用 Reading 模块。")
