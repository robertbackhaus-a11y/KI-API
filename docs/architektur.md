<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Architektur

Stand: Version 0.6.0. Zielarchitektur v1 (siehe coding-v2 decision_record im
Folder stage-runner).

## Komponenten

    Browser (index.html) --HTTP/SSE--> FastAPI (reaper_ai.main, uvicorn, 1 Prozess)
                                           |
                                           | Subprozess pro Stage
                                           v
                                stage_runner.py (eigene venv) --HTTP--> Ollama
                                           |
                                           +--> pwsh-Kindprozesse, Basic-Memory

- **GUI:** eine statische Seite, spricht nur die öffentlichen Endpunkte an
  ([api.md](api.md), [gui.md](gui.md)).
- **API:** FastAPI-App in `src/reaper_ai/main.py`. Hält den Session-Zustand im
  Speicher, persistiert ihn als JSONL und startet Stages.
- **Stage-Runner:** `stage_runner.py` ist der alleinige KI-Orchestrator. Er wird
  nicht importiert und nicht mitinstalliert, sondern als externer Subprozess mit
  eigener venv gestartet und bleibt unverändert.
- **Ollama:** einzige Modellquelle. Hauptmodell `qwen3.8-q5:latest` (role=main,
  volle REAPER-Persona); `qwen3:8b` ist das schnelle Modell für per `run_substage`
  delegierte Teilaufgaben. Die Ollama-URL des Runners ist dort fest eingetragen.

## Stage-Ablauf

1. **Startphase** (Request-Thread, unter dem Session-Lock): 404/409-Prüfung, Task-Text
   mit begrenztem Kontext bauen und als UTF-8-Datei nach `<data-dir>/tasks/` schreiben
   (umgeht das Windows-argv-Limit), Job-Object anlegen, Runner suspendiert starten
   (`CREATE_SUSPENDED | CREATE_NO_WINDOW`), dem Job zuweisen, erst dann fortsetzen.
   Aufruf: `--model … --project … --think on|off --role main --task @<datei>`.
   Events `message_received` und `stage_started` werden persistiert, `stage_started`
   geht als erstes SSE-Event in die Queue. Danach starten Pipe-Drainer und Watchdog.
2. **Pipe-Drainer:** stdout und stderr werden durchgehend geleert (voller Puffer =
   Deadlock). Behalten wird ein Diagnose-Tail von 8 KB pro Pipe. Jede stderr-Zeile
   geht live als `log_line` in die Queue und in die Mitschrift der
   finish_stage-Zusammenfassung (ab Marker `=== STAGE BEENDET`, max. 64 KB), damit
   die Zusammenfassung auch bei langen Logs nicht verloren geht.
3. **Worker-Thread** (einer pro Request, ohne Lock): wartet auf das Prozessende,
   finalisiert (Status aus dem Exit-Code, Zusammenfassung, Tails), hängt
   `stage_completed` an die JSONL an, löscht die Task-Datei und liefert das
   Terminal-Event plus Ende-Markierung an den SSE-Generator.
4. **SSE-Generator:** gibt die Queue-Einträge aus und sendet alle 5 s einen
   `heartbeat`, solange nichts anderes kommt. Eine Client-Trennung beeinflusst den
   Runner nicht.

## Begrenzte Event-Queue

`BoundedSseQueue` hat 1000 Plätze und zwei Zustellklassen. `log_line` blockiert nie
und wird bei voller Queue verworfen und gezählt. Prioritäts-Events (`stage_started`,
`log_dropped`, `stage_completed`, Ende-Markierung) verdrängen gezielt einen
`log_line`-Eintrag, nie ein anderes Prioritäts-Event. Damit bleibt der Speicher pro
Stage begrenzt und der stderr-Drainer läuft immer weiter.

## Abbruch, Watchdog, Prozessbaum

- Jede Stage läuft in einem Windows-Job-Object mit `KILL_ON_JOB_CLOSE`.
  `TerminateJobObject` beendet den ganzen Baum (Runner und alle pwsh-Kinder, auch
  später gestartete). Stirbt die API selbst, schließt das OS die Handles und beendet
  die Bäume; es bleiben keine Orphan-Prozesse.
- **POST /abort** setzt Status `aborted`, beendet den Job und wartet bis zu 30 s auf
  die Finalisierung.
- **API-Watchdog:** 25 min (`REAPER_STAGE_TIMEOUT_S`), bewusst länger als die interne
  20-min-Notbremse des Runners. Feuert nur bei echtem Hang, Status
  `watchdog_timeout`.

## Persistenz und Wiederherstellung

- Eine JSONL-Datei pro Session unter `<data-dir>/sessions/<session_id>.jsonl`,
  append-only, eine Zeile pro Event: `session_created`, `message_received`,
  `stage_started`, `stage_completed`. Statuswechsel werden angehängt, nie
  überschrieben.
- Beim Serverstart werden alle Dateien gelesen und der Zustand aus der vollen
  Event-Folge rekonstruiert. Ungültige JSON-Zeilen werden mit Warnung übersprungen,
  Dateien ohne `session_created` nicht geladen, unbekannte Event-Typen ignoriert.
- Stages, die beim Start noch `running` sind, werden `incomplete` markiert und
  nicht neu ausgeführt.

## Paket- und Verzeichnisstruktur

    pyproject.toml              Paketdefinition, Konsolenbefehl reaper-ai
    main.py                     Kompatibilitäts-Shim für den Checkout-Betrieb (nicht im Paket)
    src/reaper_ai/
      __init__.py               __version__ (einzige Versionsquelle)
      __main__.py               python -m reaper_ai
      cli.py                    run / stop / migrate / --version
      config.py                 Datenverzeichnis, config.toml, Umgebungsvariablen
      main.py                   FastAPI-App, Stage-Steuerung, Persistenz
      web/index.html            Chat-GUI
      web/vendor/               marked, DOMPurify, highlight.js (+ Lizenztexte)
    run.ps1, stop.ps1, Start-Reaper.cmd   Start/Stopp im Checkout-Betrieb

Code und GUI liegen im Paket, Laufzeitdaten im Datenverzeichnis außerhalb des
Pakets (siehe [betrieb.md](betrieb.md)). `main.py` im Paket liest ausschließlich
Umgebungsvariablen; die CLI löst Optionen und `config.toml` vorher dorthin auf.

Der Shim `main.py` im Repo-Wurzelverzeichnis setzt `REAPER_AI_HOME` auf das
Checkout-Verzeichnis (falls nicht gesetzt) und ersetzt sich selbst durch
`reaper_ai.main`. `import main` und `reaper_ai.main` sind dadurch dasselbe
Modulobjekt, sodass lokale Regressionstests Modulglobale patchen können.
