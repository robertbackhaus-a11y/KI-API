# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Configuration and data directory of Reaper-AI.

Priority (highest first): CLI option > environment variable > config.toml in the
data directory > built-in default. main.py reads only the environment variables;
the CLI (cli.py) sets them from CLI options and config.toml before the server
starts.

Environment variables / config.toml keys:

    REAPER_AI_HOME               data directory (sessions, temp tasks, config.toml, PID)
    REAPER_HOST          host    bind address          (default 127.0.0.1)
    REAPER_PORT          port    port                  (default 8734)
    REAPER_OLLAMA_URL    ollama_url     Ollama base URL (default http://127.0.0.1:11434);
                                        used by the health check AND the stage runner
    REAPER_STAGE_PYTHON          runner_python  interpreter for the runner subprocess
                                                (default: the API's own interpreter)
    REAPER_STAGE_RUNNER_SCRIPT   runner_script  external runner script instead of the
                                                integrated module (tests / rollback only)

The stage runner itself is part of the package (reaper_ai.runner.stage_runner) and
is started as `python -P -m reaper_ai.runner.stage_runner`. Its own tuning
variables (REAPER_NUM_CTX, REAPER_SHELL_OUTPUT_LIMIT, REAPER_CTX_RESPONSE_RESERVE,
REAPER_DIAG_DIR) are read by the runner directly from the inherited environment.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "reaper-ai"
CONFIG_FILE = "config.toml"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8734
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
RUNNER_MODULE = "reaper_ai.runner.stage_runner"

# config.toml key -> environment variable
CONFIG_ENV = {
    "host": "REAPER_HOST",
    "port": "REAPER_PORT",
    "ollama_url": "REAPER_OLLAMA_URL",
    "runner_python": "REAPER_STAGE_PYTHON",
    "runner_script": "REAPER_STAGE_RUNNER_SCRIPT",
}
# Keys of earlier versions that no longer have an effect (reported, not applied).
OBSOLETE_KEYS = {"runner_dir": "the stage runner is integrated; use runner_script "
                               "only to run an external runner"}


def default_data_dir() -> Path:
    """Platform-conventional location for user data, outside the installed package."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    home = os.environ.get("REAPER_AI_HOME")
    return Path(home).expanduser().resolve() if home else default_data_dir()


def runner_is_integrated() -> bool:
    """True unless an external runner script is configured."""
    return not os.environ.get("REAPER_STAGE_RUNNER_SCRIPT")


def workspace_dir(data_directory: Path) -> Path:
    """Default working directory for the stage runner (and therefore for the
    relative paths of commands run via run_powershell). A dedicated 'workspace'
    sub-directory of the data directory, deliberately NOT the data directory
    itself: a relative path a model command writes (e.g. `Out-File result.txt`,
    `git clone <repo>`) must not land among Reaper's own sessions/tasks/config
    files. See main._runner_env (REAPER_WORKDIR) and
    stage_runner.tool_run_powershell."""
    return data_directory / "workspace"


def is_packaged_pwsh(path: str) -> bool:
    """True for a PowerShell under WindowsApps (the execution-alias directory or
    the MSIX package directory) -- the packaged Store/MSIX build that escapes the
    stage's Windows job object and therefore survives abort/watchdog (R08). The
    deciding property is "packaged", verified at the process-tree level; both the
    per-user alias and the direct package path break out, so this covers both."""
    return "windowsapps" in str(path).replace("/", "\\").lower()


def _pwsh7_candidates() -> list[Path]:
    """Standard install locations of the regular (non-packaged) PowerShell 7
    executable (MSI/Win32), most specific first. Covers both a machine-scope MSI
    (<ProgramFiles>\\PowerShell\\7) and a per-user install
    (%LOCALAPPDATA%\\Programs\\PowerShell\\7) -- neither is packaged, both are kept
    in the job object."""
    out: list[Path] = []
    machine_bases = (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"), r"C:\Program Files")
    for base in machine_bases:
        if base:
            cand = Path(base) / "PowerShell" / "7" / "pwsh.exe"
            if cand not in out:
                out.append(cand)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / "Programs" / "PowerShell" / "7" / "pwsh.exe"
        if cand not in out:
            out.append(cand)
    return out


def resolve_powershell() -> str:
    """Path of a NON-PACKAGED PowerShell for the runner's run_powershell tool, or
    "" if none is available.

    R08 policy: only a non-packaged (regular Win32) PowerShell is kept in the
    stage's Windows job object, so abort/watchdog can terminate it and its
    children. The packaged Store/MSIX PowerShell escapes the job (verified at the
    process-tree level -- both the WindowsApps alias and the direct package path
    break out) and is therefore NEVER used. There is deliberately NO silent
    fallback to Windows PowerShell 5.1 either: a regular PowerShell 7 is a hard
    requirement for stage execution (see docs/operations.md). If none is found the
    caller must refuse to run a shell rather than launch an uncontained process.

    Order: REAPER_PWSH (only if it points to an existing non-packaged executable)
    -> regular PowerShell 7 (<ProgramFiles>\\PowerShell\\7\\pwsh.exe). A REAPER_PWSH
    that is missing or points to a packaged build is ignored (requirement: the
    override must not select a packaged PowerShell)."""
    if sys.platform != "win32":
        # Job-object containment is Windows-only; keep the runner usable elsewhere.
        return os.environ.get("REAPER_PWSH") or "pwsh"
    override = os.environ.get("REAPER_PWSH")
    if override and Path(override).is_file() and not is_packaged_pwsh(override):
        return override
    for cand in _pwsh7_candidates():
        if cand.is_file() and not is_packaged_pwsh(str(cand)):
            return str(cand)
    return ""


def powershell_install_hint() -> str:
    """One-line remediation shown when no suitable PowerShell 7 is found."""
    return ("Install the regular PowerShell 7 (MSI/Win32):\n"
            "    winget install --id Microsoft.PowerShell --source winget\n"
            "The Microsoft Store/MSIX PowerShell is not used because it escapes the "
            "Windows job object (process containment). Alternatively set REAPER_PWSH "
            "to a non-packaged pwsh 7 executable.")


def runner_command(data_directory: Path) -> tuple[Path, list[str], Path]:
    """(interpreter, arguments before the runner options, working directory) of
    the stage runner subprocess.

    Integrated (default): `<python> -P -m reaper_ai.runner.stage_runner`, working
    directory = the workspace sub-directory of the data directory (see
    workspace_dir); -P keeps the working directory off sys.path, so nothing in it
    can shadow a module, and the workspace is separate from Reaper's own
    sessions/tasks/config so relative model paths never pollute the data
    directory. External (REAPER_STAGE_RUNNER_SCRIPT set): `<python> <script>`,
    working directory = the script's directory, exactly as the API started the
    standalone runner before."""
    python = Path(os.environ.get("REAPER_STAGE_PYTHON") or sys.executable)
    script = os.environ.get("REAPER_STAGE_RUNNER_SCRIPT")
    if script:
        return python, [str(Path(script))], Path(script).resolve().parent
    return python, ["-P", "-m", RUNNER_MODULE], workspace_dir(data_directory)


def load_config_file(directory: Path) -> dict:
    """Reads <directory>/config.toml; a missing file = empty configuration.
    Unknown keys are ignored."""
    path = directory / CONFIG_FILE
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        raw = tomllib.load(f)
    for key, hint in OBSOLETE_KEYS.items():
        if key in raw:
            print(f"WARNING: {path}: '{key}' is obsolete and ignored ({hint}).", file=sys.stderr)
    return {k: v for k, v in raw.items() if k in CONFIG_ENV}


def apply_config_to_env(cfg: dict) -> None:
    """Transfers config.toml values into the environment WITHOUT overwriting
    environment variables that are already set."""
    for key, value in cfg.items():
        env = CONFIG_ENV[key]
        if str(value).strip() == "":
            continue  # empty entry = not configured (default applies)
        if not os.environ.get(env):
            os.environ[env] = str(value)


def ollama_url() -> str:
    return os.environ.get("REAPER_OLLAMA_URL") or DEFAULT_OLLAMA_URL


def ollama_host_port() -> tuple[str, int]:
    url = ollama_url()
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 11434
