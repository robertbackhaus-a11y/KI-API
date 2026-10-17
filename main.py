# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compatibility shim for running from the checkout (run.ps1, `uvicorn main:app`,
local regression tests with `import main`). Since packaging, the code lives in
src/reaper_ai/main.py; after import this module IS reaper_ai.main (same module
object, so tests can still patch module globals).

When running from the checkout the data directory stays this directory as before
(sessions/, tasks/), unless REAPER_AI_HOME is set. Not part of the pip package.
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("REAPER_AI_HOME", str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import reaper_ai.main as _impl  # noqa: E402

sys.modules[__name__] = _impl
