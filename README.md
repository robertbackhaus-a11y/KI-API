<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper-AI

Local chat GUI, FastAPI layer and integrated stage runner
(`reaper_ai.runner.stage_runner`, the Ollama tool-calling orchestrator).
Local single-user system, loopback only, no authentication. Since 2026-10-16
Reaper's only chat channel: Ollama serves the main model (`qwen3.8-q5:latest`,
full REAPER persona) and the fast model for subtasks delegated via `run_substage`
(`qwen3:8b`).

Status: version 0.2.0. Open and completed work packages are listed in
[ROADMAP.md](ROADMAP.md).

## Features

- Browser chat GUI without a build step: Markdown, syntax highlighting,
  copy buttons, live log, background task display, context preview,
  health status light and automatic recovery after a lost connection.
- Every message starts a stage: the integrated runner runs as its own subprocess
  (`python -m reaper_ai.runner.stage_runner`), progress arrives as an SSE stream.
  Aborting terminates the whole process tree (via a Windows job object; the runner
  uses a non-packaged PowerShell so pwsh children are contained — see R08).
- Stage runner: tool calling (PowerShell, web, Basic Memory), `run_substage`
  worker delegation, explicit `finish_stage`, context window management (B07:
  explicit `num_ctx`, bounded history, capped tool output).
- Limited context: the last 5 completed turns of a session are prepended to the
  new message.
- Sessions are stored append-only as JSONL and restored after a restart.
- Installable with `pip` directly from GitHub, with the `reaper-ai` command.

## Installation

The official and only supported installation is `pip` directly from GitHub:

    pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git

A specific release is installed via its Git tag:

    pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git@v0.2.0

This installs everything: FastAPI layer, integrated stage runner, chat GUI with
its bundled libraries and license files, and all dependencies (incl.
`basic-memory`). Reaper-AI is not published on PyPI; a manual checkout or
`pip install -e .` is only for development (see
[docs/operations.md](docs/operations.md)). Requires Python ≥ 3.12, Git on `PATH`
and a running Ollama. Running shell commands additionally requires a regular,
non-packaged **PowerShell 7** (`winget install --id Microsoft.PowerShell`); the
Microsoft Store/MSIX build is not accepted and there is no fallback to Windows
PowerShell 5.1 — see
[docs/operations.md](docs/operations.md#powershell-and-process-containment-r08).
A pip install and `reaper-ai --version` / `--help` work without PowerShell 7.

## Quick start

    reaper-ai run                  # start the server, the GUI opens in the browser
    reaper-ai stop                 # stop the server
    reaper-ai migrate C:\tools\src\Reaper-AI   # import sessions from an old checkout

GUI: http://127.0.0.1:8734. Data lives in `%LOCALAPPDATA%\reaper-ai`; port,
Ollama URL and runner tuning can be set via `config.toml`, environment variables
or CLI options.

For development in the checkout, `.\run.ps1` (or `Start-Reaper.cmd`) starts
Ollama if needed and the server with the checkout directory as data directory;
`.\stop.ps1` stops it.

## Language

English is the project language for code, documentation, CLI, API, GUI and logs.
User prompts, AI-generated answers and stored conversations keep their original
language. See the language policy in [ROADMAP.md](ROADMAP.md#development-guidelines).

## Documentation

| File | Contents |
|---|---|
| [docs/operations.md](docs/operations.md) | Installation from GitHub, updates and releases, CLI, data directory, configuration, development in the checkout |
| [docs/api.md](docs/api.md) | HTTP endpoints, SSE events, stage status |
| [docs/gui.md](docs/gui.md) | Chat GUI, Markdown security, recovery, bundled libraries |
| [docs/architecture.md](docs/architecture.md) | Components, stage flow, process-tree kill, persistence, package layout |
| [ROADMAP.md](ROADMAP.md) | Work packages and mandatory development guidelines |

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Bundled JavaScript libraries are under
their own licenses (see [docs/gui.md](docs/gui.md)).
