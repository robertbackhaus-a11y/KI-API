# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Erlaubt `python -m reaper_ai ...` als Alternative zum Konsolenskript `reaper-ai`."""

import sys

from .cli import main

sys.exit(main())
