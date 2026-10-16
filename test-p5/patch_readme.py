# -*- coding: utf-8 -*-
"""README an P5 anpassen (SSE statt synchrone JSON)."""
from pathlib import Path

p = Path(r"C:\tools\reaper-api\README.md")
raw = p.read_text(encoding="utf-8")
crlf = "\r\n" in raw
src = raw.replace("\r\n", "\n")
orig = src


def replace_once(old: str, new: str) -> None:
    global src
    n = src.count(old)
    assert n == 1, f"Anker nicht eindeutig ({n}x): {old[:90]!r}"
    src = src.replace(old, new)


replace_once("# Reaper API (P3)", "# Reaper API (P5)")

replace_once("## Stand: P3", "## Stand: P5")

replace_once(
    "- POST /sessions/{id}/messages   Stage fuer den Nutzer-Task SYNCHRON ausfuehren\n"
    "                                 (blockiert bis der Subprozess fertig ist; kein\n"
    "                                 Streaming/SSE -- das ist P5)\n",
    "- POST /sessions/{id}/messages   Stage fuer den Nutzer-Task ausfuehren und den\n"
    "                                 Fortschritt als SSE-Stream liefern (P5):\n"
    "                                 text/event-stream, sofort stage_started, alle\n"
    "                                 5 s heartbeat, am Ende genau EIN Terminal-Event\n"
    "                                 stage_completed mit dem vollen Stage-Objekt\n"
    "- POST /sessions/{id}/abort      laufende Stage kontrolliert abbrechen (P4),\n"
    "                                 Prozessbaum inkl. pwsh-Kindprozesse wird beendet\n"
    "                                 (Job-Object KILL_ON_JOB_CLOSE)\n",
)

replace_once(
    "Subprozess mit --model/--project/--task @<datei> gestartet. stdout- und stderr-Pipe\n"
    "werden DURCHGAENGIG geleert (voller Pipe-Puffer = Deadlock), es wird auf den Exit\ngewartet (API-Watchdog 25 min -- bewusst groesser als die interne 20-min-Notbremse\ndes Runners; Abbruchbehandlung ist P4), die Temp-Datei wird in Erfolg UND Fehlerfall\ngeloescht und das Ergebnis an die Session-JSONL angehaengt.\n",
    "Subprozess mit --model/--project/--task @<datei> gestartet. stdout- und stderr-Pipe\n"
    "werden DURCHGAENGIG geleert (voller Pipe-Puffer = Deadlock). Warten auf den Exit,\n"
    "Finalisierung und Persistenz laufen in einem Worker-Thread pro Request, der das\n"
    "Ergebnis ueber eine Queue an den SSE-Stream liefert (API-Watchdog 25 min --\nbewusst groesser als die interne 20-min-Notbremse des Runners; Abbruch via\nPOST /abort mit Prozessbaum-Kill ueber Job-Object). Die Temp-Datei wird in Erfolg\nUND Fehlerfall geloescht und das Ergebnis an die Session-JSONL angehaengt.\n",
)

replace_once(
    "Exit-Code -> status: 0=success, 1=error/success=false, 2=wall_clock (Runner 20 min),\n"
    "3=max_iterations; Watchdog-Timeout -> \\\"incomplete\\\" (unklar, nicht neu ausgefuehrt).\n",
    "Exit-Code -> status: 0=success, 1=error/success=false, 2=wall_clock (Runner 20 min),\n"
    "3=max_iterations; aborted (POST /abort) und watchdog_timeout (API-Watchdog) haben\n"
    "exit_code=null (kein Runner-Urteil); beim API-Neustart noch \\\"running\\\" ->\n"
    "\\\"incomplete\\\" (nicht neu ausgefuehrt).\n",
)

replace_once(
    "Response (200):\n"
    "\n"
    "    {\n"
    "      \\\"session_id\\\": \\\"...\\\",\n"
    "      \\\"stage\\\": {\n"
    "        \\\"stage_id\\\": \\\"...\\\", \\\"status\\\": \\\"success\\\", \\\"exit_code\\\": 0, \\\"pid\\\": 12345,\n"
    "        \\\"model\\\": \\\"qwen3:8b\\\", \\\"project\\\": \\\"coding-v2\\\",\n"
    "        \\\"started_at\\\": \\\"...\\\", \\\"finished_at\\\": \\\"...\\\", \\\"duration_s\\\": 12.3,\n"
    "        \\\"summary\\\": \\\"<finish_stage-Zusammenfassung>\\\",\n"
    "        \\\"stdout_tail\\\": \\\"\\\", \\\"stderr_tail\\\": \\\"<letzte ~8 KB>\\\"\n"
    "      }\n"
    "    }\n",
    "Response (200, Content-Type: text/event-stream) -- Standard-SSE-Rahmen\n"
    "(event: <typ>\\ndata: <eine JSON-Zeile>\\n\\n), Events ankommen BEVOR die Stage fertig ist:\n"
    "\n"
    "    event: stage_started            # sofort nach der Startphase\n"
    "    data: {\\\"stage_id\\\": \\\"...\\\", \\\"status\\\": \\\"running\\\", \\\"exit_code\\\": null, \\\"pid\\\": 12345,\n"
    "           \\\"model\\\": \\\"qwen3:8b\\\", \\\"project\\\": \\\"coding-v2\\\", \\\"started_at\\\": \\\"...\\\"}\n"
    "\n"
    "    event: heartbeat                # alle 5 s, solange die Stage laeuft\n"
    "    data: {\\\"elapsed_s\\\": 5.0}\n"
    "\n"
    "    event: stage_completed          # genau EIN Terminal-Event, volles Stage-Objekt\n"
    "    data: {\\\"stage_id\\\": \\\"...\\\", \\\"status\\\": \\\"success\\\", \\\"exit_code\\\": 0, \\\"pid\\\": 12345,\n"
    "           \\\"model\\\": \\\"qwen3:8b\\\", \\\"project\\\": \\\"coding-v2\\\",\n"
    "           \\\"started_at\\\": \\\"...\\\", \\\"finished_at\\\": \\\"...\\\", \\\"duration_s\\\": 12.3,\n"
    "           \\\"summary\\\": \\\"<finish_stage-Zusammenfassung>\\\",\n"
    "           \\\"stdout_tail\\\": \\\"\\\", \\\"stderr_tail\\\": \\\"<letzte ~8 KB>\\\"}\n"
    "\n"
    "Kein Token-Streaming, keine Tool-Events (bewusst vertagt, Zielarchitektur v1).\n"
    "Eine Client-Trennung waehrend der Stage bricht den Runner NICHT ab -- Abbruch\n"
    "bleibt POST /sessions/{id}/abort.\n",
)

replace_once(
    "    curl.exe -i -X POST http://127.0.0.1:8734/sessions/<id>/messages ^\n"
    "      -H \\\"Content-Type: application/json\\\" ^\n"
    "      -d \\\"{\\\\\\\"message\\\\\\\": \\\\\\\"Sag kurz Hallo und ruf dann finish_stage auf\\\\\\\"}\\\"\n",
    "    curl.exe -N -i -X POST http://127.0.0.1:8734/sessions/<id>/messages ^\n"
    "      -H \\\"Content-Type: application/json\\\" ^\n"
    "      -d \\\"{\\\\\\\"message\\\\\\\": \\\\\\\"Sag kurz Hallo und ruf dann finish_stage auf\\\\\\\"}\\\"\n",
)

replace_once(
    "Vertagt (Phasen der Zielarchitektur): P4 Fehler-/Abbruchbehandlung (Prozessbaum-Kill\n"
    "inkl. pwsh-Kindprozesse, sauberes Watchdog-Verhalten), P5 SSE-Statusausgabe,\n"
    "Begrenzter Chat-Kontext fuer Folgeauftraege (der Task-Slot erhaelt vorerst die\n"
    "Nachricht unveraendert). stage_runner.py wird NICHT importiert, sondern als separater\n"
    "Subprozess aufgerufen und bleibt unveraendert.\n",
    "Vertagt: Begrenzter Chat-Kontext fuer Folgeauftraege (der Task-Slot erhaelt vorerst\ndie Nachricht unveraendert) -- eigenes Paket nach P5. stage_runner.py wird NICHT\nimportiert, sondern als separater Subprozess aufgerufen und bleibt unveraendert.\n",
)

if src == orig:
    raise SystemExit("NICHTS AENDERTE -- Abbruch")
p.write_text(src.replace("\n", "\r\n") if crlf else src, encoding="utf-8")
print(f"OK README (CRLF={crlf})")
