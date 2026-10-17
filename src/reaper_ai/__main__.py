# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Allows `python -m reaper_ai ...` as an alternative to the `reaper-ai` console script."""

import sys

from .cli import main

sys.exit(main())
