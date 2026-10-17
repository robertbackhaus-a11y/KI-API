# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

r"""Integrated stage runner (formerly the standalone C:\tools\stage-runner).

The runner is the sole agent orchestrator (Ollama tool loop, run_substage, Basic
Memory, web tools). The API (reaper_ai.main) never imports it to execute stages;
it starts it as a separate subprocess:

    python -m reaper_ai.runner.stage_runner --model ... --project ... --task @file

Keep this package free of imports so `python -m` stays cheap and isolated.
"""
