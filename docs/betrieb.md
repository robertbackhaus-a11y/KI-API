<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Installation, CLI und Konfiguration

Stand: Version 0.6.0. Quellen: `src/reaper_ai/cli.py`, `src/reaper_ai/config.py`,
`pyproject.toml`, `run.ps1`, `stop.ps1`.

## Voraussetzungen

- Windows 11, Python ≥ 3.11
- Ollama mit den Modellen `qwen3.8-q5:latest` und `qwen3:8b`
- Stage-Runner mit eigener venv, Default `C:\tools\stage-runner`
- Python-Abhängigkeiten: `fastapi`, `uvicorn` (feste Versionen in `requirements.txt`)

## Installation als Paket

    pip install reaper-ai          # nach Veröffentlichung; lokal: pip install -e .

## CLI

    reaper-ai run      [--host H] [--port P] [--data-dir D] [--ollama-url U]
                       [--runner-dir R] [--no-browser]
    reaper-ai stop     [--data-dir D]
    reaper-ai migrate  QUELLVERZEICHNIS [--data-dir D]
    reaper-ai --version

`python -m reaper_ai …` ist gleichwertig.

**run** startet die FastAPI-App mit uvicorn im Vordergrund (ein Prozess, keine
Worker), schreibt eine PID-Datei und öffnet die GUI im Browser, sobald der Port
antwortet (`--no-browser` unterdrückt das). Beim Start werden GUI-URL,
Datenverzeichnis, Runner-Pfad und Ollama-URL ausgegeben, mit Warnung, wenn Runner
oder Ollama fehlen. Ollama wird nicht gestartet (anders als `run.ps1`). Der Start
wird verweigert, wenn laut PID-Datei schon eine Instanz läuft oder der Port belegt
ist. Bei einer Bind-Adresse außerhalb von Loopback erscheint eine Warnung, da es
keine Authentifizierung gibt.

**stop** beendet den in der PID-Datei eingetragenen Prozessbaum (`taskkill /T /F`),
aber nur, wenn dessen Port noch lauscht (Schutz gegen wiederverwendete PIDs).
Laufende Stages sterben über ihr Job-Object mit und erscheinen nach dem nächsten
Start als `incomplete`. Veraltete PID-Dateien werden entfernt.

**migrate** kopiert `<quelle>/sessions/*.jsonl` (oder `*.jsonl` direkt aus der
Quelle) nach `<data-dir>/sessions/`. Die Quelle bleibt unverändert, vorhandene
Dateien im Ziel werden nie überschrieben. Danach Server neu starten.

    reaper-ai migrate C:\tools\src\Reaper-AI

## Datenverzeichnis

Laufzeitdaten liegen außerhalb des Pakets. Default `%LOCALAPPDATA%\reaper-ai`
(sonst `$XDG_DATA_HOME/reaper-ai`), änderbar mit `--data-dir` oder `REAPER_AI_HOME`.

    <data-dir>\sessions\*.jsonl     Sessions (append-only)
    <data-dir>\tasks\               temporäre Task-Dateien
    <data-dir>\config.toml          optionale Konfiguration
    <data-dir>\reaper-ai.pid        PID-Datei für reaper-ai stop

## Konfiguration

Priorität: CLI-Option > Umgebungsvariable > `config.toml` > Default.

| config.toml | Umgebungsvariable | CLI | Default |
|---|---|---|---|
| `host` | `REAPER_HOST` | `--host` | `127.0.0.1` |
| `port` | `REAPER_PORT` | `--port` | `8734` |
| `ollama_url` | `REAPER_OLLAMA_URL` | `--ollama-url` | `http://127.0.0.1:11434` |
| `runner_dir` | `REAPER_STAGE_RUNNER_DIR` | `--runner-dir` | `C:\tools\stage-runner` |
| `runner_python` | `REAPER_STAGE_PYTHON` | – | `<runner_dir>\.venv\Scripts\python.exe` |
| `runner_script` | `REAPER_STAGE_RUNNER_SCRIPT` | – | `<runner_dir>\stage_runner.py` |
| – | `REAPER_AI_HOME` | `--data-dir` | `%LOCALAPPDATA%\reaper-ai` |
| – | `REAPER_STAGE_TIMEOUT_S` | – | `1500` (nur für Regressionstests) |

Beispiel `config.toml`:

    host          = "127.0.0.1"
    port          = 8734
    ollama_url    = "http://127.0.0.1:11434"
    runner_dir    = "C:/tools/stage-runner"

Unbekannte Schlüssel und leere Werte werden ignoriert. `ollama_url` wirkt nur auf
die Health-Ampel und die Startprüfung von `reaper-ai run`; `stage_runner.py` nutzt
weiterhin seine fest eingetragene URL (`http://localhost:11434`).

## Entwicklung im Checkout

    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\pip install -e .

- `.\run.ps1` bzw. `Start-Reaper.cmd` startet bei Bedarf Ollama, dann
  `uvicorn main:app` auf `127.0.0.1:8734` und öffnet die GUI. `main.py` im
  Wurzelverzeichnis ist ein Kompatibilitäts-Shim auf `reaper_ai.main` (siehe
  [architektur.md](architektur.md)); Datenverzeichnis ist dabei das
  Checkout-Verzeichnis (`sessions/`, `tasks/`).
- `.\stop.ps1` beendet den Server über den Port, mit `-Ollama` auch Ollama.
- `reaper-ai stop` beendet einen per `run.ps1` gestarteten Server nicht (keine
  PID-Datei). `reaper-ai run` aus dem Checkout nutzt `%LOCALAPPDATA%\reaper-ai`,
  nicht `sessions/` im Repo (außer mit `--data-dir .`).
- Lokale Regressionstests (`test-*`) bleiben außerhalb von Git (`.gitignore`).

## Veröffentlichung auf PyPI (vorbereitet, nicht durchgeführt)

In einer separaten Build-venv:

    python -m pip install build twine
    python -m build                 # dist\reaper_ai-<version>.tar.gz und .whl
    python -m twine check dist\*
    python -m twine upload --repository testpypi dist\*   # zuerst TestPyPI
    python -m twine upload dist\*                          # erst nach Freigabe

Die Version steht ausschließlich in `src/reaper_ai/__init__.py` (`__version__`).
