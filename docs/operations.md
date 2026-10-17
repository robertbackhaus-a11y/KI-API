<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Installation, CLI and Configuration

Status: version 0.2.1 + integrated stage runner. Sources: `src/reaper_ai/cli.py`,
`src/reaper_ai/config.py`, `src/reaper_ai/runner/stage_runner.py`, `pyproject.toml`,
`run.ps1`, `stop.ps1`.

## Requirements

- Windows 11, Python ≥ 3.12 (required by `basic-memory`)
- Ollama with the models `qwen3.8-q5:latest` and `qwen3:8b`
- **A regular, non-packaged PowerShell 7** (MSI/Win32,
  `C:\Program Files\PowerShell\7\pwsh.exe`) for the runner's `run_powershell` tool.
  This is a hard requirement for running shell commands. The Microsoft Store / MSIX
  build is **not** accepted (it escapes the stage's job object, so its processes can
  survive abort/watchdog), and there is **no** fallback to Windows PowerShell 5.1.
  Install it with `winget install --id Microsoft.PowerShell --source winget`. A pip
  install, `reaper-ai --version` and `--help` work without it; the requirement is
  checked at `reaper-ai run` (clear hint) and enforced before shell execution — see
  [Process containment](#powershell-and-process-containment-r08).
- Python dependencies, installed with the package (pinned versions in
  `requirements.txt`): `fastapi`, `uvicorn`, and for the stage runner `requests`
  and `basic-memory` (`>=0.23.2,<0.24`, because the runner uses Basic Memory's
  internal client API). `basic-memory` 0.23.2 itself pins the FastMCP
  pre-release `fastmcp==4.0.0b1` (which pins `fastmcp-slim==4.0.0b1`), so
  `pyproject.toml` also lists `fastmcp` and `fastmcp-slim` (`>=4.0.0b1,<5`) as
  direct dependencies: uv only accepts a pre-release that a first-party
  requirement names explicitly. Basic Memory keeps its configuration and
  projects in the user profile (`~/.basic-memory`), so all installations of the same user share
  the same memory projects.
- No separate stage-runner directory or venv any more: the runner is part of the
  package and runs with the same interpreter as the API.

## Installation (binding standard)

Reaper-AI is installed exclusively with `pip` directly from the GitHub
repository:

    pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git

A specific release is installed via its Git tag (`@<tag>`):

    pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git@v0.2.1

Without a tag, the current `master` is installed. Recommended is a dedicated
venv, e.g.:

    py -3.12 -m venv C:\tools\reaper-ai-venv
    C:\tools\reaper-ai-venv\Scripts\pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git@v0.2.1
    C:\tools\reaper-ai-venv\Scripts\reaper-ai run

The installation is complete; nothing has to be copied or checked out by hand:

- the `reaper_ai` package with the FastAPI app, CLI and configuration,
- the integrated stage runner (`reaper_ai.runner.stage_runner`),
- the chat GUI (`web/index.html`) and the bundled libraries in `web/vendor/`
  including their license texts,
- the project license (`LICENSE`, in the package metadata),
- all Python dependencies from `pyproject.toml` (`fastapi`, `uvicorn`,
  `requests`, `basic-memory`),
- the console command `reaper-ai`.

`uv pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git[@<tag>]`
into a uv venv installs the same set, including `basic-memory`; no
`--prerelease=allow` or other resolver flag is needed (fixed in v0.2.1; v0.2.0
still fails under uv because of `basic-memory`'s `fastmcp==4.0.0b1` pin).

Rules:

- **No PyPI.** Reaper-AI is not published on PyPI; `pip install reaper-ai`
  is not a valid installation path.
- **No manual checkout, no `pip install -e .`** for users. Editable installs
  from a checkout are only for development (see
  [Development in the checkout](#development-in-the-checkout)).
- Additional requirements for the installation: Git on `PATH` (pip clones the
  repository with it) and network access to github.com.
- Use a short venv path (e.g. `C:\tools\…`): a dependency of `basic-memory`
  contains very long file paths, which can exceed the Windows path limit
  (`MAX_PATH`) in deeply nested directories.

### Updating and switching versions

    pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git@v0.2.1

installs the given release over the existing one. pip only replaces an installed
package when the version number differs: reinstalling the same version (e.g. the
current `master` while it still carries the released version number) is
silently skipped. In that case force it:

    pip install --force-reinstall --no-deps git+https://github.com/robertbackhaus-a11y/Reaper-AI.git

Sessions and configuration live in the data directory and are not touched by an
update or reinstall.

### Releases

A release is a Git tag `v<version>` on `master`:

1. Set the version in `src/reaper_ai/__init__.py` (`__version__`, the single
   version source) and update the status lines in the documentation.
2. Commit, create the annotated tag `v<version>`, push the commit and the tag.
3. Optionally build wheel and sdist (`python -m build` in a build venv) and
   attach them to the GitHub release.

Every release tag is then installable with `…Reaper-AI.git@v<version>`. Tags are
never moved or reused once pushed.

## CLI

    reaper-ai run      [--host H] [--port P] [--data-dir D] [--ollama-url U]
                       [--no-browser]
    reaper-ai stop     [--data-dir D]
    reaper-ai migrate  SOURCE_DIR [--data-dir D]
    reaper-ai --version

`python -m reaper_ai …` is equivalent.

**run** starts the FastAPI app with uvicorn in the foreground (one process, no
workers), writes a PID file and opens the GUI in the browser as soon as the port
answers (`--no-browser` suppresses this). On start it prints the GUI URL, data
directory, stage runner (`integrated (reaper_ai.runner.stage_runner)` or the
external script) and Ollama URL, with a warning if the runner or Ollama is
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
    <data-dir>\workspace\           working directory of the stage runner
    <data-dir>\config.toml          optional configuration
    <data-dir>\reaper-ai.pid        PID file for reaper-ai stop

`<data-dir>\workspace` is the working directory of the stage runner process and
therefore the default directory for `run_powershell` commands without an explicit
`workdir` (passed to the runner as `REAPER_WORKDIR`). It is deliberately a
sub-directory, **not** the data directory itself: a relative path a model command
writes (e.g. `Out-File result.txt`, `git clone <repo>`) must not land among
Reaper's own sessions/tasks/config files. (Previously the data directory itself was
the working directory.)

## Configuration

Priority: CLI option > environment variable > `config.toml` > default.

| config.toml | Environment variable | CLI | Default |
|---|---|---|---|
| `host` | `REAPER_HOST` | `--host` | `127.0.0.1` |
| `port` | `REAPER_PORT` | `--port` | `8734` |
| `ollama_url` | `REAPER_OLLAMA_URL` | `--ollama-url` | `http://127.0.0.1:11434` |
| `runner_python` | `REAPER_STAGE_PYTHON` | – | the API's own interpreter |
| `runner_script` | `REAPER_STAGE_RUNNER_SCRIPT` | – | unset = integrated runner |
| – | `REAPER_AI_HOME` | `--data-dir` | `%LOCALAPPDATA%\reaper-ai` |
| – | `REAPER_STAGE_TIMEOUT_S` | – | `1500` (regression tests only) |
| – | `REAPER_NUM_CTX` | – | `65536` (Ollama `num_ctx`, sent per request, never raised automatically) |
| – | `REAPER_SHELL_OUTPUT_LIMIT` | – | `8000` (max. characters per tool result, head+tail kept, marked) |
| – | `REAPER_CTX_RESPONSE_RESERVE` | – | `4096` (tokens reserved for the next model response) |
| – | `REAPER_DIAG_DIR` | – | unset (opt-in: full tool outputs are written there for diagnosis) |
| – | `REAPER_PWSH` | – | unset (explicit PowerShell for `run_powershell`; must point at a **non-packaged** pwsh 7 — a packaged/Store target is ignored. See [Process containment](#powershell-and-process-containment-r08)) |
| – | `REAPER_WORKDIR` | – | `<data-dir>\workspace` (set by the API; default cwd for `run_powershell` relative paths) |

The tuning variables above (`REAPER_NUM_CTX`, `REAPER_SHELL_OUTPUT_LIMIT`,
`REAPER_CTX_RESPONSE_RESERVE`, `REAPER_DIAG_DIR`, `REAPER_PWSH`) are read by the
stage runner from the inherited environment (set them before `reaper-ai run`).
`REAPER_WORKDIR` is set by the API itself; override it only for a standalone
runner.

## PowerShell and process containment (R08)

Every stage runs inside a Windows job object (`KILL_ON_JOB_CLOSE`) so that an
abort (`POST /abort`) or the watchdog kills the whole process tree, including the
`pwsh` processes started by `run_powershell`. This containment works **only** for
a non-packaged (regular Win32) PowerShell, because a normal child process inherits
its parent's job automatically.

The Microsoft Store / MSIX build of PowerShell 7 does **not** inherit the job: its
real process is activated into its own package-managed job outside Reaper's, so
`TerminateJobObject` never reaches it and the `pwsh` (and anything it launched) can
survive an abort/watchdog. This was verified at the process-tree level and affects
the packaged build reached **either** via the per-user execution alias
(`%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe`) **or** via the direct package
path (`C:\Program Files\WindowsApps\Microsoft.PowerShell_*\pwsh.exe`) — it is a
property of the packaged build, not of the alias.

**Policy:** a regular, non-packaged PowerShell 7 is required. The runner resolves
the executable in this order (`reaper_ai.config.resolve_powershell`):

1. `REAPER_PWSH`, only if it points at an existing **non-packaged** executable (a
   packaged target is ignored);
2. the regular PowerShell 7, `C:\Program Files\PowerShell\7\pwsh.exe` (machine MSI)
   or `%LOCALAPPDATA%\Programs\PowerShell\7\pwsh.exe` (per-user install).

There is **no** fallback to the packaged Store/MSIX build and **no** fallback to
Windows PowerShell 5.1. If none is found, `run_powershell` returns a clear error and
launches nothing (no uncontained shell is ever started), and `reaper-ai run` prints
a warning with the install command at startup. The resolved executable is logged at
the start of every stage (`powershell: <path> (non-packaged, job-contained)`).

Reaper never installs anything automatically. Install the regular PowerShell 7:

    winget install --id Microsoft.PowerShell --source winget

If the Microsoft Store build is already present, winget may report "no upgrade
available" and skip the MSI; install the machine-wide MSI explicitly from an
**elevated** terminal:

    winget install --id Microsoft.PowerShell --source winget --scope machine --force

The runner then picks up `C:\Program Files\PowerShell\7\pwsh.exe` automatically. A
pip install, `reaper-ai --version` and `reaper-ai --help` work without PowerShell 7;
only running shell commands requires it.

`runner_script` is only for an external runner: the regression tests use it for a
fake runner, and it is a rollback path to an old standalone `stage_runner.py`
(then also set `runner_python` to that runner's venv). Without it, the API starts
`<python> -P -m reaper_ai.runner.stage_runner` with the workspace directory
(`<data-dir>\workspace`) as working directory and the package's parent directory
prepended to `PYTHONPATH`, so the subprocess always runs the same package copy as
the API. The former
`runner_dir` / `REAPER_STAGE_RUNNER_DIR` / `--runner-dir` were removed; an old
`runner_dir` entry in `config.toml` or a set `REAPER_STAGE_RUNNER_DIR` is ignored
with a warning.

Example `config.toml`:

    host          = "127.0.0.1"
    port          = 8734
    ollama_url    = "http://127.0.0.1:11434"

Unknown keys and empty values are ignored. `ollama_url` / `REAPER_OLLAMA_URL` is
used by the health light, the startup check of `reaper-ai run` and the stage
runner (which appends `/api/chat`). If it is not set at all, the runner falls back
to `http://localhost:11434`.

## Development in the checkout

Only for developing Reaper-AI itself, not for installing it:

    git clone https://github.com/robertbackhaus-a11y/Reaper-AI.git
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
- Run one stage directly, without the API:
  `python -m reaper_ai.runner.stage_runner --model qwen3:8b --project coding-v2 --task "..."`
  (`--help` lists `--role`, `--think`, `--num-ctx`, `--shell-output-limit`).

## Checking the package contents

The GitHub installation builds the package from the files tracked in Git, so
every package file (runner, GUI, `web/vendor/`, license texts) must be committed.
Package data is declared in `pyproject.toml` (`[tool.setuptools.package-data]`);
subpackages such as `reaper_ai.runner` are found automatically. Before a release,
in a separate build venv:

    python -m pip install build
    python -m build                 # dist\reaper_ai-<version>.tar.gz and .whl

and check that the wheel contains `reaper_ai/runner/stage_runner.py`,
`reaper_ai/web/index.html` and all files of `reaper_ai/web/vendor/`. Then test
`pip install git+…@<tag>` in a fresh venv.

PyPI is not used; there is no upload step.
