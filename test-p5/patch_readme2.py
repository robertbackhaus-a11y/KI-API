# -*- coding: utf-8 -*-
"""README an P5 anpassen (SSE statt synchrone JSON) -- zeilenbasiert mit
eindeutigen Zeilenmarkern; Neue Inhalte als rohe Triple-Strings (kein Escaping)."""
from pathlib import Path

p = Path(r"C:\tools\reaper-api\README.md")
raw = p.read_bytes().decode("utf-8")
crlf = "\r\n" in raw
lines = raw.replace("\r\n", "\n").split("\n")


def find_line(needle: str, what: str) -> int:
    hits = [i for i, l in enumerate(lines) if needle in l]
    assert len(hits) == 1, f"{what}: {len(hits)} Treffer fuer {needle!r}"
    return hits[0]


def replace_range(start: int, end: int, new_block: str, what: str) -> None:
    block = new_block.rstrip("\n").split("\n")
    print(f"[{what}] Zeilen {start+1}..{end+1} ({end-start+1}) -> {len(block)}")
    lines[start:end + 1] = block


s = find_line("# Reaper API (P3)", "Titel")
replace_range(s, s, "# Reaper API (P5)\n", "Titel")

s = find_line("## Stand: P3", "Stand")
replace_range(s, s, "## Stand: P5\n", "Stand")

s = find_line("- POST /sessions/{id}/messages", "messages-Bullet")
assert lines[s + 1].startswith(" " * 33) and "blockiert" in lines[s + 1], "messages-Bullet Fortsetzung nicht erwartet"
replace_range(s, s + 2, r'''- POST /sessions/{id}/messages   Stage fuer den Nutzer-Task ausfuehren und den
                                 Fortschritt als SSE-Stream liefern (P5):
                                 text/event-stream, sofort stage_started, alle
                                 5 s heartbeat, am Ende genau EIN Terminal-Event
                                 stage_completed mit dem vollen Stage-Objekt
- POST /sessions/{id}/abort      laufende Stage kontrolliert abbrechen (P4);
                                 Prozessbaum inkl. pwsh-Kindprozesse wird ueber
                                 Job-Object (KILL_ON_JOB_CLOSE) beendet
''', "messages-Bullet")

s = find_line("Ablauf: Task-Text wird in eine Temp-Datei", "Ablauf")
assert lines[s + 6].endswith("angehaengt."), f"Ablauf-Ende nicht erwartet: {lines[s+6]!r}"
replace_range(s, s + 6, r'''Ablauf: Task-Text wird in eine Temp-Datei (tasks/, UTF-8; wegen Windows-argv-Limit)
geschrieben und C:\tools\stage-runner\stage_runner.py (eigene venv dort) als
Subprozess mit --model/--project/--task @<datei> gestartet. stdout- und stderr-Pipe
werden DURCHGAENGIG geleert (voller Pipe-Puffer = Deadlock). Warten auf den Exit,
Finalisierung und Persistenz laufen in einem Worker-Thread pro Request, der das
Ergebnis ueber eine Queue an den SSE-Stream liefert (API-Watchdog 25 min --
bewusst groesser als die interne 20-min-Notbremse des Runners; Abbruch via
POST /abort mit Prozessbaum-Kill ueber Job-Object). Die Temp-Datei wird in Erfolg
UND Fehlerfall geloescht und das Ergebnis an die Session-JSONL angehaengt.
''', "Ablauf")

s = find_line("Exit-Code -> status:", "Exit-Code")
assert lines[s + 1].startswith("3=max_iterations"), f"Exit-Code-Fortsetzung nicht erwartet: {lines[s+1]!r}"
replace_range(s, s + 1, '''Exit-Code -> status: 0=success, 1=error/success=false, 2=wall_clock (Runner 20 min),
3=max_iterations; aborted (POST /abort) und watchdog_timeout (API-Watchdog) haben
exit_code=null (kein Runner-Urteil); beim API-Neustart noch "running" ->
"incomplete" (nicht neu ausgefuehrt).
''', "Exit-Code")

s = find_line("Response (200):", "Response-Block")
e = next(i for i in range(s + 1, len(lines)) if lines[i] == "    }")
replace_range(s, e, '''Response (200, Content-Type: text/event-stream) -- Standard-SSE-Rahmen
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
''', "Response-Block")

s = find_line("curl.exe -i -X POST http://127.0.0.1:8734/sessions/<id>/messages", "curl")
replace_range(s, s, "    curl.exe -N -i -X POST http://127.0.0.1:8734/sessions/<id>/messages ^\n", "curl")

s = find_line("Vertagt (Phasen der Zielarchitektur):", "Vertagt")
assert lines[s + 4].endswith("unveraendert."), f"Vertagt-Ende nicht erwartet: {lines[s+4]!r}"
replace_range(s, s + 4, '''Vertagt: Begrenzter Chat-Kontext fuer Folgeauftraege (der Task-Slot erhaelt vorerst
die Nachricht unveraendert) -- eigenes Paket nach P5. stage_runner.py wird NICHT
importiert, sondern als separater Subprozess aufgerufen und bleibt unveraendert.
''', "Vertagt")

out = "\n".join(lines)
if crlf:
    out = out.replace("\n", "\r\n")
p.write_bytes(out.encode("utf-8"))
print(f"OK README geschrieben (CRLF={crlf}, {len(lines)} Zeilen)")


