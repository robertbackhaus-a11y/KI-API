<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# HTTP-API

Stand: Version 0.6.0. Quelle: `src/reaper_ai/main.py`.

Lokales Single-User-System, nur Loopback, keine Authentifizierung. Kein
OpenAPI-Schema, keine `/docs`- oder `/redoc`-Seite (bewusst abgeschaltet).
Standardadresse: `http://127.0.0.1:8734`.

## Übersicht

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/` | Chat-GUI (`src/reaper_ai/web/index.html`), siehe [gui.md](gui.md) |
| GET | `/vendor/<datei>` | lokal mitgelieferte GUI-Bibliotheken (statisch) |
| GET | `/health` | Diagnose für die Ampel der GUI |
| POST | `/sessions` | neue Session anlegen |
| GET | `/sessions/{id}` | Session lesen |
| GET | `/sessions/{id}/context` | Vorschau des begrenzten Kontexts für die nächste Nachricht |
| POST | `/sessions/{id}/messages` | Stage ausführen, Fortschritt als SSE-Stream |
| POST | `/sessions/{id}/abort` | laufende Stage abbrechen |

## GET /health

    {"api": "ok", "ollama": "ok" | "down", "stage_runner": "ok" | "missing"}

Rein diagnostisch. Ollama wird nur per TCP-Connect auf `ollama_url` geprüft (siehe
[betrieb.md](betrieb.md)), der Runner nur auf Vorhandensein von Python und Skript.
Die API ruft Ollama selbst nie auf; das tut ausschließlich `stage_runner.py`.

## POST /sessions

Antwort `201` mit dem Session-Objekt. `session_id` ist eine UUID v4. Legt
`<data-dir>/sessions/<session_id>.jsonl` mit dem Event `session_created` an.

## GET /sessions/{id}

Antwort `200`:

    {
      "session_id": "...",
      "created_at": "...",
      "messages": [{"role": "user", "content": "...", "stage_id": "...", "ts": "..."}],
      "stages":   [<Stage-Objekt>, ...]
    }

`404` bei unbekannter Id. `messages[].content` ist immer die originale Nachricht,
nie der um Kontext erweiterte Task-Text.

## GET /sessions/{id}/context

Zeigt, welchen Kontext eine nächste Nachricht vorangestellt bekäme. Startet keine
Stage. `404` bei unbekannter Id.

    {"turns_included": 3, "max_turns": 5, "max_chars": 6000, "preview": "Bisheriger Verlauf ..."}

Begrenzter Kontext: der Task-Text enthält neben der aktuellen Nachricht die letzten
5 abgeschlossenen Turns der Session (Nutzer-Nachricht plus Stage-Zusammenfassung),
gekappt auf 6000 Zeichen (`CONTEXT_MAX_TURNS`, `CONTEXT_MAX_CHARS`). Kein
Vollverlauf; dauerhaftes Wissen bleibt Aufgabe des Modells per `memory_write`.

## POST /sessions/{id}/messages

Request-Body (JSON):

    {
      "message": "<Nutzer-Task-Text>",   # Pflicht, nicht leer und nicht nur Leerraum
      "model":   "qwen3.8-q5:latest",    # optional, Default: Reapers Hauptmodell
      "project": "coding-v2",            # optional, Default: Basic-Memory-Projekt
      "think":   false                   # optional, Default false (Reasoning aus)
    }

`think` wird immer explizit als `--think on|off` an den Runner gereicht. Ohne
diesen Schalter würde der Runner `auto` nehmen und Qwen3/Q5 praktisch unbegrenzt
nachdenken.

Fehler vor dem Stream-Start: `404` unbekannte Session, `409` es läuft bereits eine
Stage in dieser Session (eine Stage pro Session gleichzeitig), `422` ungültiger Body.

Antwort `200`, `Content-Type: text/event-stream`, Standard-SSE-Rahmen
(`event: <typ>`, `data:` eine JSON-Zeile, Leerzeile als Rahmenende). Die Events
kommen, während die Stage läuft:

    event: stage_started            # sofort nach der Startphase, immer zuerst
    data: {"stage_id": "...", "status": "running", "exit_code": null, "pid": 12345,
           "model": "qwen3.8-q5:latest", "project": "coding-v2", "started_at": "..."}

    event: log_line                 # live, je vollständige stderr-Zeile des Runners
    data: {"text": "[12:00:01] TOOL ..."}

    event: heartbeat                # alle 5 s ohne anderes Event
    data: {"elapsed_s": 5.0}

    event: log_dropped              # nur bei Überlast, einmal direkt vor stage_completed
    data: {"count": 42}

    event: stage_completed          # genau EIN Terminal-Event mit dem vollen Stage-Objekt
    data: {"stage_id": "...", "status": "success", "exit_code": 0, "pid": 12345,
           "model": "...", "project": "...", "started_at": "...", "finished_at": "...",
           "duration_s": 12.3, "summary": "<finish_stage-Zusammenfassung>",
           "stdout_tail": "", "stderr_tail": "<letzte ~8 KB>"}

- `log_line` ist roher Logtext und best-effort: die Event-Queue pro Stage hat 1000
  Plätze; bei langsamem oder getrenntem Client werden Log-Zeilen verworfen und
  gezählt (`log_dropped`). `stage_started` und `stage_completed` kommen immer an.
  Einzelne Zeilen werden bei 64 KB gekappt (Suffix ` ...[Zeile gekuerzt]`).
- Kein Token-Streaming und keine strukturierten Tool-Events (Zielarchitektur v1).
- Eine Client-Trennung bricht die Stage nicht ab. Abbruch nur über `/abort`.

Beispiel:

    curl.exe -N -i -X POST http://127.0.0.1:8734/sessions/<id>/messages ^
      -H "Content-Type: application/json" ^
      -d "{\"message\": \"Sag kurz Hallo und ruf dann finish_stage auf\"}"

## POST /sessions/{id}/abort

Beendet den Prozessbaum der laufenden Stage und wartet auf deren Finalisierung.
Antwort `200`: `{"session_id": "...", "stage": <Stage-Objekt mit status "aborted">}`.
`404` unbekannte Session, `409` keine laufende Stage, `500` wenn die Stage 30 s nach
dem Kill nicht finalisiert ist.

## Stage-Status

| status | exit_code | Bedeutung |
|---|---|---|
| `running` | null | Stage läuft |
| `success` | 0 | Runner erfolgreich |
| `error` | 1 | Runner-Fehler bzw. `success=false` |
| `wall_clock` | 2 | interne 20-min-Notbremse des Runners |
| `max_iterations` | 3 | Iterationslimit des Runners |
| `aborted` | null | Nutzer-Abbruch über `/abort` |
| `watchdog_timeout` | null | API-Watchdog (25 min) hat die Stage beendet |
| `incomplete` | null | Stage lief noch, als die API neu startete; wird nicht neu ausgeführt |
| `unknown_rc_N` | N | defensiv, sollte nicht vorkommen |

Der Status kommt ausschließlich aus dem Exit-Code. `summary` ist reiner
Anzeigetext: die finish_stage-Zusammenfassung ab dem Marker `=== STAGE BEENDET`.

## Testen

    curl.exe -i -X POST http://127.0.0.1:8734/sessions
    curl.exe -i http://127.0.0.1:8734/sessions/<session_id>
    curl.exe -i http://127.0.0.1:8734/sessions/keine-id    # erwartet: 404
    curl.exe -i http://127.0.0.1:8734/health
