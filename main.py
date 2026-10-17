# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Kompatibilitaets-Shim fuer den Checkout-Betrieb (run.ps1, `uvicorn main:app`,
lokale Regressionstests mit `import main`). Der Code liegt seit der Paketierung in
src/reaper_ai/main.py; dieses Modul IST danach reaper_ai.main (gleiches Modulobjekt,
damit Tests weiterhin Modulglobale patchen koennen).

Datenverzeichnis im Checkout-Betrieb bleibt wie bisher dieses Verzeichnis
(sessions/, tasks/), sofern REAPER_AI_HOME nicht gesetzt ist. Nicht Teil des
pip-Pakets.
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("REAPER_AI_HOME", str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import reaper_ai.main as _impl  # noqa: E402

sys.modules[__name__] = _impl
