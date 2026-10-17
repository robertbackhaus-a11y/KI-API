<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper-AI

Lokale Chat-GUI und FastAPI-Schicht vor dem Stage-Runner (`stage_runner.py`).
Lokales Single-User-System, nur Loopback, keine Authentifizierung. Seit 2026-10-16
Reapers einziger Chat-Kanal: Ollama bedient das Hauptmodell (`qwen3.8-q5:latest`,
volle REAPER-Persona) und das schnelle Modell für per `run_substage` delegierte
Teilaufgaben (`qwen3:8b`).

Stand: Version 0.6.0. Offene und erledigte Arbeitspakete stehen in
[ROADMAP.md](ROADMAP.md).

## Funktionen

- Chat-GUI im Browser ohne Build-Schritt: Markdown, Syntax-Highlighting,
  Kopieren-Buttons, Live-Log, Anzeige von Hintergrundaufgaben, Kontext-Vorschau,
  Health-Ampel und automatische Wiederaufnahme nach Verbindungsverlust.
- Jede Nachricht startet eine Stage: `stage_runner.py` läuft als eigener Subprozess,
  der Fortschritt kommt als SSE-Stream. Abbruch beendet den ganzen Prozessbaum.
- Begrenzter Kontext: die letzten 5 abgeschlossenen Turns einer Session werden der
  neuen Nachricht vorangestellt.
- Sessions werden append-only als JSONL gespeichert und nach einem Neustart
  wiederhergestellt.
- Installierbar als pip-Paket mit dem Befehl `reaper-ai`.

## Schnellstart

    pip install -e .               # bzw. nach Veröffentlichung: pip install reaper-ai
    reaper-ai run                  # Server starten, GUI öffnet sich im Browser
    reaper-ai stop                 # Server beenden
    reaper-ai migrate C:\tools\src\Reaper-AI   # alte Checkout-Sessions übernehmen

GUI: http://127.0.0.1:8734. Ollama muss laufen, der Stage-Runner liegt
standardmäßig unter `C:\tools\stage-runner`. Daten liegen in
`%LOCALAPPDATA%\reaper-ai`; Pfade, Port und Ollama-URL sind über `config.toml`,
Umgebungsvariablen oder CLI-Optionen einstellbar.

Im Checkout startet `.\run.ps1` (bzw. `Start-Reaper.cmd`) Ollama bei Bedarf und den
Server mit dem Checkout-Verzeichnis als Datenverzeichnis; `.\stop.ps1` beendet ihn.

## Dokumentation

| Datei | Inhalt |
|---|---|
| [docs/betrieb.md](docs/betrieb.md) | Installation, CLI, Datenverzeichnis, Konfiguration, Entwicklung im Checkout, PyPI |
| [docs/api.md](docs/api.md) | HTTP-Endpunkte, SSE-Events, Stage-Status |
| [docs/gui.md](docs/gui.md) | Chat-GUI, Markdown-Sicherheit, Recovery, mitgelieferte Bibliotheken |
| [docs/architektur.md](docs/architektur.md) | Komponenten, Stage-Ablauf, Prozessbaum-Kill, Persistenz, Paketstruktur |
| [ROADMAP.md](ROADMAP.md) | Arbeitspakete und verbindliche Entwicklungsregeln |

## Lizenz

GPL-3.0-or-later, siehe [LICENSE](LICENSE). Mitgelieferte JavaScript-Bibliotheken
stehen unter ihren eigenen Lizenzen (siehe [docs/gui.md](docs/gui.md)).
