<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper-AI – Entwicklungsroadmap

**Repository:** `C:\tools\src\Reaper-AI`  
**Plattform:** Windows 11 / Python / FastAPI / Ollama  
**Installation:** Python-Pakete ausschließlich über `pip` in `.venv`  
**Architektur:** Lokales Single-User-System

## Verbindliche Regeln

1. Bestehende FastAPI-Architektur beibehalten.
2. `stage_runner.py` bleibt alleiniger KI-Orchestrator.
3. Keine zusätzlichen Dienste, Datenbanken, Container oder Frontend-Build-Systeme.
4. Neue Python-Abhängigkeiten ausschließlich über `pip` installieren und in `requirements.txt` mit festen Versionen dokumentieren.
5. JavaScript-/CSS-Bibliotheken bei Bedarf als lokale, versionierte Dateien einbinden. Keine CDN-Abhängigkeiten im Betrieb, kein npm.
6. Bestehende Funktionen erhalten und nach jeder Phase Regressionstests durchführen.
7. Testskripte lokal behalten und von Git ausschließen.
8. Änderungen pro Arbeitspaket getrennt umsetzen. Nicht automatisch committen, pushen oder deployen.
9. Copyright- und SPDX-Header in neuen Quellcodedateien beachten.
10. Keine unnötigen Refactorings.

## Verbindliche Entwicklungsregeln

Gilt automatisch für sämtliche zukünftigen Bugfixes, Features, Entwicklungsphasen und Arbeitspakete:

1. Nach jeder Implementierung müssen `README.md`, `ROADMAP.md` und betroffene Dateien unter `docs/` geprüft und bei Bedarf aktualisiert werden.
2. Neue Funktionen, API-Endpunkte, Konfigurationsparameter, CLI-Befehle und Architekturänderungen müssen dokumentiert werden.
3. Abgeschlossene Arbeitspakete in der Roadmap als erledigt markieren.
4. Veraltete oder widersprüchliche Dokumentation korrigieren.
5. README bleibt die kompakte Projektübersicht; technische Details gehören nach `docs/`.
6. Dokumentation muss dem tatsächlichen Implementierungsstand entsprechen.
7. Ein Arbeitspaket gilt erst als abgeschlossen, wenn Code, Tests und Dokumentation konsistent sind.
8. Neue und geänderte Quellcodedateien müssen den Copyright-/SPDX-Header enthalten.
9. Testdateien bleiben lokal und außerhalb der Git-Versionsverwaltung.

Zuordnung in `docs/`: `api.md` (Endpunkte, SSE, Status), `gui.md` (Chat-GUI, Bibliotheken), `betrieb.md` (Installation, CLI, Konfiguration), `architektur.md` (Komponenten, Abläufe, Persistenz, Paketstruktur).

## Phase 0 – Verbleibende Bugfixes

| ID | Aufgabe | Priorität | Stand |
|---|---|---|---|
| B01 | Globale Begrenzung paralleler GPU-intensiver Haupt-Stages | P2 | offen |
| B02 | Fehlerbehandlung bei fehlgeschlagenem `TerminateJobObject()` prüfen und absichern | P2 | offen |
| B03 | Thread-Synchronisierung der Session-Zustände prüfen und korrigieren | P2 | offen |
| B04 | SSE-Recovery-Polling und unnötiges Neuzeichnen optimieren | P2 | teilweise: Recovery pollt alle 2 s, Verlauf wird nur bei geänderter Nachrichtenzahl/Stage-Status neu aufgebaut, Markdown-Rendering gecacht; der Neuaufbau selbst ist weiterhin vollständig |
| B05 | JSONL-Recovery bei unvollständigen Events härten | P3 | offen |
| B06 | Speicher- und Handle-Freigabe bei längeren Sessions prüfen | P3 | offen |

**Abnahme:** Alle bisherigen 73 Regressionstests und neu ergänzte Tests bestanden. Keine Prozessleichen, Deadlocks oder unkontrollierten parallelen GPU-Läufe.

## Phase P – Paketierung als pip-Paket

Eigenes Arbeitspaket vor den weiteren GUI-Erweiterungen (Phase 1 ff.). Ziel: Installation und Start wie bei Chainlit.

```
pip install reaper-ai
reaper-ai run
reaper-ai stop
reaper-ai --version
```

| ID | Aufgabe | Stand |
|---|---|---|
| P01 | `pyproject.toml` und `src/`-Layout (`src/reaper_ai/`) | umgesetzt |
| P02 | CLI-Einstiegspunkt `reaper-ai` über `[project.scripts]` (`run`, `stop`, `migrate`, `--version`) | umgesetzt |
| P03 | FastAPI-App, Web-GUI und `web/vendor/` vollständig im Paket | umgesetzt |
| P04 | Sessions, Temp-Tasks, Konfiguration und PID-Datei außerhalb des Pakets (`%LOCALAPPDATA%\reaper-ai`, `--data-dir` / `REAPER_AI_HOME`) | umgesetzt |
| P05 | Stage-Runner weiterhin externer Subprozess, `stage_runner.py` unverändert | umgesetzt |
| P06 | Runner-Pfad und Ollama-Endpunkt konfigurierbar (CLI > Env > `config.toml` > Default) | umgesetzt |
| P07 | Lokale Installation `pip install -e .` in frischer venv getestet | umgesetzt |
| P08 | Wheel gebaut und Installation in frischer venv getestet | umgesetzt |
| P09 | Bestehende Sessions übernehmen: `reaper-ai migrate <checkout>` (nur kopieren); Checkout-Betrieb (`run.ps1`) nutzt weiter `sessions/` im Repo | umgesetzt |
| P10 | PyPI-Veröffentlichung vorbereitet (`python -m build`, `twine check`), Upload erst nach Freigabe, zuerst TestPyPI | vorbereitet |

Keine neuen Backend-Dienste, keine zusätzlichen Frameworks, keine funktionalen Änderungen am Stage-Runner. Hinweis: `ollama_url` wirkt auf Health-Ampel und Startprüfung; `stage_runner.py` nutzt weiterhin seine fest eingetragene URL.

**Abnahme:** Alle Regressionstests grün, `reaper-ai run/stop/--version` aus editierbarer und aus Wheel-Installation funktionsfähig, bestehende Sessions erhalten.

## Phase 1 – Chat-Basis

| ID | Feature | Stand |
|---|---|---|
| F01 | Markdown-Rendering | umgesetzt (marked 15.0.12, GFM, bereinigt mit DOMPurify 3.4.16) |
| F02 | Syntax-Highlighting für Code | umgesetzt (highlight.js 11.12.0 inkl. PowerShell, Theme hell/dunkel) |
| F03 | Copy-Buttons für Antworten und Codeblöcke | umgesetzt |
| F04 | Session-Liste und Chat-Wechsel | offen |
| F05 | Sessions umbenennen und löschen | offen |
| F06 | Chat-Export als Markdown | offen |

Markdown-Ausgabe gegen unsicheres HTML absichern. Rendering nur bei geänderten Nachrichten. Lokale JavaScript-Bibliotheken ohne npm verwenden. F01 bis F03 erfüllen das: Bibliotheken liegen in `src/reaper_ai/web/vendor/` (Details in `docs/gui.md`).

## Phase 2 – Dateiunterstützung

| ID | Feature |
|---|---|
| F07 | Datei-Upload über FastAPI |
| F08 | Ablage von Dateien je Session |
| F09 | Bildvorschau und Dateianhänge im Chat |
| F10 | Dateigrößen- und Typprüfung |

`python-multipart` über `pip` installieren. Keine generelle Dateiverarbeitung in der API; die fachliche Verarbeitung bleibt beim Runner. Uploads liegen im Datenverzeichnis (Phase P), nicht im Paket.

## Phase 3 – Agenten-Monitoring

| ID | Feature |
|---|---|
| F11 | Strukturierte Stage-/Substage-Events |
| F12 | Tool-Aufrufe mit Status und Laufzeit |
| F13 | Technischen Status und fachliches Ergebnis getrennt anzeigen |
| F14 | Fehler- und Abbruchursachen nachvollziehbar darstellen |

Strukturierte Ereignisse statt Logtext-Parsing. Bestehendes SSE erweitern, kein neues Protokoll oder Message-Broker.

## Phase 4 – Chat-Workflows

| ID | Feature |
|---|---|
| F15 | Nachrichten bearbeiten und neu ausführen |
| F16 | Suche über vorhandene Sessions |
| F17 | Wiederverwendbare Prompt-Vorlagen |

Bei erneuter Ausführung ausdrücklich berücksichtigen, dass Tool-Aktionen wiederholt werden können.

## Phase 5 – Memory und Kontrolle

| ID | Feature |
|---|---|
| F18 | Vorhandene Memory-Einträge anzeigen |
| F19 | Memory-Einträge gezielt korrigieren |
| F20 | Human-in-the-loop für kritische Tool-Aktionen |

Keine zweite Memory-Datenbank. Human-in-the-loop benötigt eine echte Pausierung des Runners vor der Tool-Ausführung.

## Phase 6 – Abschluss

- Vollständiger Regressionstest.
- Integrationstest mit realem Ollama und Stage-Runner.
- Prüfung von Abort, Watchdog, Recovery und parallelen Sessions.
- Prüfung sämtlicher neuer Dateien auf Copyright-Header.
- `requirements.txt`, README und `docs/` aktualisieren.
- Installation in einer frischen `.venv` ausschließlich über `pip` testen.
- Abschließenden technischen Review erstellen.

## Ausführungsanweisung für Claude

Arbeite die Roadmap in der angegebenen Reihenfolge ab, aber implementiere immer nur **ein Arbeitspaket pro Durchlauf**.

Vor jeder Änderung den tatsächlichen Code prüfen. Bereits implementierte Funktionen nicht erneut entwickeln.

Nach jedem Arbeitspaket:

1. Geänderte Dateien benennen.
2. Testergebnisse dokumentieren.
3. Dokumentation gemäß den verbindlichen Entwicklungsregeln aktualisieren.
4. Verbleibende Risiken nennen.
5. Nächstes Arbeitspaket vorschlagen.

Keine eigenständigen Commits, Pushes oder Deployments. Keine Enterprise-Erweiterungen.
