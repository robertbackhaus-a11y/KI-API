<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper API

HTTP-Geruest fuer den Chat-Frontend-Zugang zu stage_runner.py (Zielarchitektur v1,
siehe coding-v2 decision_record im Folder stage-runner). Lokales Single-User-System,
nur Loopback, keine Authentifizierung.

Seit 2026-10-16 Reapers EINZIGER Chat-Kanal (Jan wurde dafuer abgeloest): Ollama
bedient sowohl das Hauptmodell (qwen3.8-q5:latest, role=main -- volle REAPER-
Persona, siehe stage_runner.py) als auch das schnelle Worker-Modell fuer per
run_substage delegierte Teilaufgaben (Default qwen3:8b).

## Stand: P5 + einseitige Chat-GUI

- GET  /                         liefert web/index.html aus (einseitige Chat-GUI,
                                 kein Build-Schritt, kein Framework). Spricht
                                 AUSSCHLIESSLICH die unten dokumentierten Endpunkte
                                 an; kennt stage_runner.py/Ollama/Basic-Memory nicht.
                                 Session-Id wird im localStorage des Browsers
                                 gehalten -- Reload laedt denselben Verlauf per
                                 GET /sessions/{id} neu.
- POST /sessions                 neue Session anlegen (session_id = UUID v4); legt
                                 sessions/<session_id>.jsonl mit der ersten Zeile an
- GET  /sessions/{id}            Session lesen; 404 wenn unbekannt
- POST /sessions/{id}/messages   Stage fuer den Nutzer-Task ausfuehren und den
                                 Fortschritt als SSE-Stream liefern (P5):
                                 text/event-stream, sofort stage_started, alle
                                 5 s heartbeat, am Ende genau EIN Terminal-Event
                                 stage_completed mit dem vollen Stage-Objekt
- POST /sessions/{id}/abort      laufende Stage kontrolliert abbrechen (P4);
                                 Prozessbaum inkl. pwsh-Kindprozesse wird ueber
                                 Job-Object (KILL_ON_JOB_CLOSE) beendet
- Persistenz: eine JSONL-Datei pro Session unter sessions/, append-only, eine Zeile
  pro Event. Event-Typen: session_created, message_received, stage_started,
  stage_completed. Beim Neustart werden alle Dateien eingelesen und der Zustand aus
  der vollen Event-Folge rekonstruiert; Stages, die dann noch "running" waren,
  werden "incomplete" markiert (NICHT automatisch neu ausgefuehrt).

## POST /sessions/{id}/messages

Request-Body (JSON):

    {
      "message": "<Nutzer-Task-Text>",     # Pflicht, nicht leer
      "model":   "qwen3.8-q5:latest",      # optional, Default wie gezeigt (Reapers Hauptmodell)
      "project": "coding-v2",              # optional, Default wie gezeigt (Basic-Memory-Projekt)
      "think":   false                     # optional, Default false (Reasoning AUS); true =
                                           # Modell denkt mit (Ollama 'think'); Qwen3/Q5 ohne
                                           # Angabe = eigener Modell-Default (praktisch unbegrenzt)
    }

Ablauf: Task-Text wird in eine Temp-Datei (tasks/, UTF-8; wegen Windows-argv-Limit)
geschrieben und C:\tools\stage-runner\stage_runner.py (eigene venv dort) als
Subprozess mit --model/--project/--think on|off/--task @<datei> gestartet. think wird
IMMER explizit durchgereicht (Default aus): ohne den Key wuerde der Runner sein 'auto'
nehmen und das Modell seinen eigenen Reasoning-Default nutzen (Qwen3/Q5: praktisch
unbegrenzt). stdout- und stderr-Pipe
werden DURCHGAENGIG geleert (voller Pipe-Puffer = Deadlock). Warten auf den Exit,
Finalisierung und Persistenz laufen in einem Worker-Thread pro Request, der das
Ergebnis ueber eine Queue an den SSE-Stream liefert (API-Watchdog 25 min --
bewusst groesser als die interne 20-min-Notbremse des Runners; Abbruch via
POST /abort mit Prozessbaum-Kill ueber Job-Object). Die Temp-Datei wird in Erfolg
UND Fehlerfall geloescht und das Ergebnis an die Session-JSONL angehaengt.

Exit-Code -> status: 0=success, 1=error/success=false, 2=wall_clock (Runner 20 min),
3=max_iterations; aborted (POST /abort) und watchdog_timeout (API-Watchdog) haben
exit_code=null (kein Runner-Urteil); beim API-Neustart noch "running" ->
"incomplete" (nicht neu ausgefuehrt).
Der Status kommt NUR aus dem Exit-Code; die finish_stage-Zusammenfassung wird als
Anzeigetext aus dem stderr-Tail extrahiert (Marker "=== STAGE BEENDET").

Response (200, Content-Type: text/event-stream) -- Standard-SSE-Rahmen
(event: <typ>, data: eine JSON-Zeile, leere Zeile als Rahmenende); die Events
kommen BEVOR die Stage fertig ist:

    event: stage_started            # sofort nach der Startphase
    data: {"stage_id": "...", "status": "running", "exit_code": null, "pid": 12345,
           "model": "qwen3:8b", "project": "coding-v2", "started_at": "..."}

    event: heartbeat                # alle 5 s, solange die Stage laeuft
    data: {"elapsed_s": 5.0}

    event: stage_completed          # genau EIN Terminal-Event, volles Stage-Objekt
    data: {"stage_id": "...", "status": "success", "exit_code": 0, "pid": 12345,
           "model": "qwen3:8b", "project": "coding-v2",
           "started_at": "...", "finished_at": "...", "duration_s": 12.3,
           "summary": "<finish_stage-Zusammenfassung>",
           "stdout_tail": "", "stderr_tail": "<letzte ~8 KB>"}

Kein Token-Streaming, keine Tool-Events (bewusst vertagt, Zielarchitektur v1).
Eine Client-Trennung waehrend der Stage bricht den Runner NICHT ab -- Abbruch
bleibt POST /sessions/{id}/abort.

Waehrend eine Stage laeuft liefert ein POST auf dieselbe Session 409 (eine Stage
pro Session gleichzeitig). Beispiel:

    curl.exe -N -i -X POST http://127.0.0.1:8734/sessions/<id>/messages ^
      -H "Content-Type: application/json" ^
      -d "{\"message\": \"Sag kurz Hallo und ruf dann finish_stage auf\"}"

Begrenzter Kontext: der Task-Slot bekommt nicht mehr NUR die aktuelle Nachricht,
sondern zusaetzlich die letzten CONTEXT_MAX_TURNS (Default 5) abgeschlossenen
Turns dieser Session als kurze Orientierung (Nutzer-Nachricht + Stage-
Zusammenfassung, bei CONTEXT_MAX_CHARS gekappt) -- siehe _build_task_text in
main.py. KEIN Vollverlauf, dauerhaftes Wissen bleibt weiterhin Aufgabe des
Modells per memory_write. Die persistierte Nachrichten-Historie
(session["messages"]) enthaelt weiterhin die ORIGINALE, kurze Nachricht.

stage_runner.py wird NICHT importiert, sondern als separater Subprozess
aufgerufen und bleibt unveraendert.

## Einmalig einrichten

    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt

## Starten

    .\run.ps1

Server laeuft auf http://127.0.0.1:8734 (uvicorn, einzelner Prozess, keine Worker).

## Testen

    curl.exe -i -X POST http://127.0.0.1:8734/sessions
    curl.exe -i http://127.0.0.1:8734/sessions/<session_id>
    curl.exe -i http://127.0.0.1:8734/sessions/keine-id    # erwartet: 404