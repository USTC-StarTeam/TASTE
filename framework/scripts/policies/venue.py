from __future__ import annotations

import re
from typing import Any


def venue_slug(value: Any, default: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or default
