# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Konfiguration und Datenverzeichnis von Reaper-AI.

Prioritaet (hoechste zuerst): CLI-Option > Umgebungsvariable > config.toml im
Datenverzeichnis > eingebauter Default. main.py liest ausschliesslich die
Umgebungsvariablen; die CLI (cli.py) setzt sie vor dem Serverstart aus
CLI-Optionen und config.toml.

Umgebungsvariablen / config.toml-Schluessel:

    REAPER_AI_HOME               Datenverzeichnis (Sessions, Temp-Tasks, config.toml, PID)
    REAPER_HOST          host    Bind-Adresse          (Default 127.0.0.1)
    REAPER_PORT          port    Port                  (Default 8734)
    REAPER_OLLAMA_URL    ollama_url     Ollama-Endpunkt (Default http://127.0.0.1:11434)
    REAPER_STAGE_RUNNER_DIR      runner_dir     Verzeichnis von stage_runner.py
    REAPER_STAGE_PYTHON          runner_python  Python der Runner-venv
    REAPER_STAGE_RUNNER_SCRIPT   runner_script  Pfad zu stage_runner.py
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
DEFAULT_RUNNER_DIR = Path(r"C:\tools\stage-runner")

# config.toml-Schluessel -> Umgebungsvariable
CONFIG_ENV = {
    "host": "REAPER_HOST",
    "port": "REAPER_PORT",
    "ollama_url": "REAPER_OLLAMA_URL",
    "runner_dir": "REAPER_STAGE_RUNNER_DIR",
    "runner_python": "REAPER_STAGE_PYTHON",
    "runner_script": "REAPER_STAGE_RUNNER_SCRIPT",
}


def default_data_dir() -> Path:
    """Plattformueblicher Ort fuer Nutzerdaten, ausserhalb des installierten Pakets."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    home = os.environ.get("REAPER_AI_HOME")
    return Path(home).expanduser().resolve() if home else default_data_dir()


def default_runner_python(runner_dir: Path) -> Path:
    if sys.platform == "win32":
        return runner_dir / ".venv" / "Scripts" / "python.exe"
    return runner_dir / ".venv" / "bin" / "python"


def load_config_file(directory: Path) -> dict:
    """Liest <directory>/config.toml; fehlende Datei = leere Konfiguration.
    Unbekannte Schluessel werden ignoriert."""
    path = directory / CONFIG_FILE
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return {k: v for k, v in raw.items() if k in CONFIG_ENV}


def apply_config_to_env(cfg: dict) -> None:
    """Uebertraegt config.toml-Werte in die Umgebung, OHNE bereits gesetzte
    Umgebungsvariablen zu ueberschreiben."""
    for key, value in cfg.items():
        env = CONFIG_ENV[key]
        if str(value).strip() == "":
            continue  # leerer Eintrag = nicht konfiguriert (Default greift)
        if not os.environ.get(env):
            os.environ[env] = str(value)


def ollama_url() -> str:
    return os.environ.get("REAPER_OLLAMA_URL") or DEFAULT_OLLAMA_URL


def ollama_host_port() -> tuple[str, int]:
    url = ollama_url()
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 11434
