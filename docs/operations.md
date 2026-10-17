<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Installation, CLI and Configuration

Status: version 0.1.0. Sources: `src/reaper_ai/cli.py`, `src/reaper_ai/config.py`,
`pyproject.toml`, `run.ps1`, `stop.ps1`.

## Requirements

- Windows 11, Python ≥ 3.11
- Ollama with the models `qwen3.8-q5:latest` and `qwen3:8b`
- Stage runner with its own venv, default `C:\tools\stage-runner`
- Python dependencies: `fastapi`, `uvicorn` (pinned versions in `requirements.txt`)

## Installation as a package

    pip install reaper-ai          # once published; locally: pip install -e .

## CLI

    reaper-ai run      [--host H] [--port P] [--data-dir D] [--ollama-url U]
                       [--runner-dir R] [--no-browser]
    reaper-ai stop     [--data-dir D]
    reaper-ai migrate  SOURCE_DIR [--data-dir D]
    reaper-ai --version

`python -m reaper_ai …` is equivalent.

**run** starts the FastAPI app with uvicorn in the foreground (one process, no
workers), writes a PID file and opens the GUI in the browser as soon as the port
answers (`--no-browser` suppresses this). On start it prints the GUI URL, data
directory, runner path and Ollama URL, with a warning if the runner or Ollama is
missing. Ollama is not started (unlike `run.ps1`). Starting is refused if a PID
file says an instance is already running or the port is in use. A bind address
outside loopback triggers a warning, since there is no authentication.

**stop** terminates the process tree recorded in the PID file (`taskkill /T /F`),
but only if its port is still listening (protection against reused PIDs). Running
stages die along with it via their job object and show up as `incomplete` after
the next start. Stale PID files are removed.

**migrate** copies `<source>/sessions/*.jsonl` (or `*.jsonl` directly from the
source) to `<data-dir>/sessions/`. The source stays unchanged, existing files in
the target are never overwritten. Restart the server afterwards.

    reaper-ai migrate C:\tools\src\Reaper-AI

## Data directory

Runtime data lives outside the package. Default `%LOCALAPPDATA%\reaper-ai`
(otherwise `$XDG_DATA_HOME/reaper-ai`), changeable with `--data-dir` or
`REAPER_AI_HOME`.

    <data-dir>\sessions\*.jsonl     sessions (append-only)
    <data-dir>\tasks\               temporary task files
    <data-dir>\config.toml          optional configuration
    <data-dir>\reaper-ai.pid        PID file for reaper-ai stop

## Configuration

Priority: CLI option > environment variable > `config.toml` > default.

| config.toml | Environment variable | CLI | Default |
|---|---|---|---|
| `host` | `REAPER_HOST` | `--host` | `127.0.0.1` |
| `port` | `REAPER_PORT` | `--port` | `8734` |
| `ollama_url` | `REAPER_OLLAMA_URL` | `--ollama-url` | `http://127.0.0.1:11434` |
| `runner_dir` | `REAPER_STAGE_RUNNER_DIR` | `--runner-dir` | `C:\tools\stage-runner` |
| `runner_python` | `REAPER_STAGE_PYTHON` | – | `<runner_dir>\.venv\Scripts\python.exe` |
| `runner_script` | `REAPER_STAGE_RUNNER_SCRIPT` | – | `<runner_dir>\stage_runner.py` |
| – | `REAPER_AI_HOME` | `--data-dir` | `%LOCALAPPDATA%\reaper-ai` |
| – | `REAPER_STAGE_TIMEOUT_S` | – | `1500` (regression tests only) |

Example `config.toml`:

    host          = "127.0.0.1"
    port          = 8734
    ollama_url    = "http://127.0.0.1:11434"
    runner_dir    = "C:/tools/stage-runner"

Unknown keys and empty values are ignored. `ollama_url` only affects the health
light and the startup check of `reaper-ai run`; `stage_runner.py` still uses its
hard-coded URL (`http://localhost:11434`).

## Development in the checkout

    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\pip install -e .

- `.\run.ps1` or `Start-Reaper.cmd` starts Ollama if needed, then
  `uvicorn main:app` on `127.0.0.1:8734`, and opens the GUI. `main.py` in the
  root directory is a compatibility shim for `reaper_ai.main` (see
  [architecture.md](architecture.md)); the data directory is the checkout
  directory (`sessions/`, `tasks/`).
- `.\stop.ps1` stops the server via its port, with `-Ollama` also Ollama.
- `reaper-ai stop` does not stop a server started via `run.ps1` (no PID file).
  `reaper-ai run` from the checkout uses `%LOCALAPPDATA%\reaper-ai`, not
  `sessions/` in the repo (unless `--data-dir .` is given).
- Local regression tests (`test-*`) stay outside Git (`.gitignore`).

## Publishing to PyPI (prepared, not performed)

In a separate build venv:

    python -m pip install build twine
    python -m build                 # dist\reaper_ai-<version>.tar.gz and .whl
    python -m twine check dist\*
    python -m twine upload --repository testpypi dist\*   # TestPyPI first
    python -m twine upload dist\*                          # only after approval

The version is defined only in `src/reaper_ai/__init__.py` (`__version__`).
