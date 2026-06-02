"""Helper used by `memex serve --reload`.

uvicorn's --reload mode needs an importable factory or app object. We can't
pass an already-built FastAPI instance, so we re-read `MEMEX_ROOT` from the
environment (set by the CLI) and rebuild on every reload.
"""

from __future__ import annotations

import os

from memex.server.api import build_app


def reload_app():
    return build_app(os.environ.get("MEMEX_ROOT"))
