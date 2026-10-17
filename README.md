<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper-AI

Local chat GUI and FastAPI layer in front of the stage runner (`stage_runner.py`).
Local single-user system, loopback only, no authentication. Since 2026-10-16
Reaper's only chat channel: Ollama serves the main model (`qwen3.8-q5:latest`,
full REAPER persona) and the fast model for subtasks delegated via `run_substage`
(`qwen3:8b`).

Status: version 0.1.0. Open and completed work packages are listed in
[ROADMAP.md](ROADMAP.md).

## Features

- Browser chat GUI without a build step: Markdown, syntax highlighting,
  copy buttons, live log, background task display, context preview,
  health status light and automatic recovery after a lost connection.
- Every message starts a stage: `stage_runner.py` runs as its own subprocess,
  progress arrives as an SSE stream. Aborting terminates the whole process tree.
- Limited context: the last 5 completed turns of a session are prepended to the
  new message.
- Sessions are stored append-only as JSONL and restored after a restart.
- Installable as a pip package with the `reaper-ai` command.

## Quick start

    pip install -e .               # or, once published: pip install reaper-ai
    reaper-ai run                  # start the server, the GUI opens in the browser
    reaper-ai stop                 # stop the server
    reaper-ai migrate C:\tools\src\Reaper-AI   # import sessions from an old checkout

GUI: http://127.0.0.1:8734. Ollama must be running; the stage runner is expected
at `C:\tools\stage-runner` by default. Data lives in `%LOCALAPPDATA%\reaper-ai`;
paths, port and Ollama URL can be set via `config.toml`, environment variables or
CLI options.

In the checkout, `.\run.ps1` (or `Start-Reaper.cmd`) starts Ollama if needed and
the server with the checkout directory as data directory; `.\stop.ps1` stops it.

## Language

English is the project language for code, documentation, CLI, API, GUI and logs.
User prompts, AI-generated answers and stored conversations keep their original
language. See the language policy in [ROADMAP.md](ROADMAP.md#development-guidelines).

## Documentation

| File | Contents |
|---|---|
| [docs/operations.md](docs/operations.md) | Installation, CLI, data directory, configuration, development in the checkout, PyPI |
| [docs/api.md](docs/api.md) | HTTP endpoints, SSE events, stage status |
| [docs/gui.md](docs/gui.md) | Chat GUI, Markdown security, recovery, bundled libraries |
| [docs/architecture.md](docs/architecture.md) | Components, stage flow, process-tree kill, persistence, package layout |
| [ROADMAP.md](ROADMAP.md) | Work packages and mandatory development guidelines |

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Bundled JavaScript libraries are under
their own licenses (see [docs/gui.md](docs/gui.md)).
