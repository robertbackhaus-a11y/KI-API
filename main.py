# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

r"""Reaper API -- P5: HTTP-Geruest + Sessions + JSONL-Persistenz + Stage als Subprozess
+ Fehler-/Abbruchbehandlung (Prozessbaum-Kill, aktiver Watchdog, POST /abort)
+ SSE-Statusausgabe fuer POST /sessions/{id}/messages.

Zielarchitektur v1 (coding-v2, stage-runner, decision_record):
- POST /sessions                  neue Chat-Session anlegen (session_created-Event)
- GET  /sessions/{id}             Session lesen, 404 wenn unbekannt
- POST /sessions/{id}/messages    Stage fuer den Nutzer-Task ausfuehren und den
                                  Fortschritt als SSE-Stream liefern (P5):
                                  text/event-stream, sofort ein stage_started-
                                  Event, alle 5 s ein heartbeat-Event, am Ende
                                  genau EIN Terminal-Event stage_completed mit
                                  dem vollen Stage-Objekt (status/exit_code/
                                  finished_at/duration_s/summary/stdout_tail/
                                  stderr_tail). 404/409 bleiben VOR dem
                                  Stream-Start. Kein Token-Streaming, keine
                                  Tool-Events (bewusst, Zielarchitektur v1).
- POST /sessions/{id}/abort       laufende Stage kontrolliert abbrechen (P4),
                                  Prozessbaum inkl. pwsh-Kindprozesse wird beendet

Eine Stage = das bestehende C:\tools\stage-runner\stage_runner.py als separater
Subprozess (eigene venv dort). Der Runner-Code bleibt unveraendert und wird NICHT
importiert. Exit-Codes 0-3 sind die EINZIGE Statusquelle (Zielarchitektur); abgebrochene
Stages (Nutzer-Abort oder API-Watchdog) haben exit_code=null, weil kein Runner-Urteil
vorliegt. Die stdout/stderr-Pipes werden durchgaengig geleert (voller Pipe-Puffer =
Deadlock) und liefern Diagnose-Tails plus die finish_stage-Zusammenfassung als
Anzeigetext.

Prozessbaum-Kill (Windows): jede Stage laeuft in einem Windows-Job-Object mit
KILL_ON_JOB_CLOSE. TerminateJobObject beendet damit den ganzen Baum atomar
(Hauptprozess + alle pwsh-Kindprozesse, auch spaeter gestartete). Wenn der API-Prozess
selbst stirbt, schliessen sich die Job-Handles und das OS toetet die Baeume -- keine
Orphan-Prozesse mehr nach einem API-Crash.

Status-Wortschatz: success/error/wall_clock/max_iterations (Exit-Code 0-3),
aborted (Nutzer via /abort), watchdog_timeout (API-Watchdog), incomplete (nur noch:
Stage lief beim API-Neustart), unknown_rc_N (defensiv, darf normal nicht vorkommen).

Persistenz: eine JSONL-Datei pro Session unter <app-dir>/sessions/<session_id>.jsonl,
append-only, eine Zeile pro Event. Statuswechsel werden angehaengt, nie ueberschrieben.
Beim Serverstart werden alle Dateien eingelesen und der Zustand aus der vollen
Event-Folge rekonstruiert; Stages, die dann noch "running" waren, werden "incomplete"
markiert und NICHT automatisch neu ausgefuehrt.

P5-Ablauf: POST /messages laeuft die Startphase UNTER dem Session-Lock synchron
durch (404/409-Pruefung bleibt VOR dem Stream-Start; Subprozess + Kill-Job +
Drainer + Watchdog + JSONL-Events message_received/stage_started) und gibt dann
eine StreamingResponse zurueck. Warten auf den Exit, Finalisierung und
Persistenz des Terminal-Events laufen in einem Worker-Thread pro Request und
liefern das Ergebnis als Queue-Items an den async SSE-Generator (stage_started
wird vorab im Request-Thread gepusht). Client-Trennung waehrend der Stage
aendert nichts am Runner-Lauf -- Abbruch bleibt POST /abort.

Nicht in P5 (bewusst vertagt): Begrenzter Chat-Kontext fuer Folgeauftraege (der
Task-Slot erhaelt vorerst die Nachricht unveraendert).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from starlette.responses import FileResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reaper-api")

APP_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = APP_DIR / "sessions"
TASK_TMP_DIR = APP_DIR / "tasks"  # Temp-Taskdateien fuer --task @datei (Windows-argv-Limit)

# Stage-Runner: bestehender Code, eigene venv, eigenes Verzeichnis -- nur als Subprozess.
# Per Env-Var ueberschreibbar NUR fuer Regressionstests (test-queue/), damit sie einen
# Fake-Runner statt des echten stage_runner.py/Ollama verwenden koennen; Produktions-
# Default (keine Env-Vars gesetzt) ist unveraendert exakt wie vorher.
STAGE_RUNNER_DIR = Path(r"C:\tools\stage-runner")
STAGE_PYTHON = Path(os.environ["REAPER_STAGE_PYTHON"]) if os.environ.get("REAPER_STAGE_PYTHON") \
    else STAGE_RUNNER_DIR / ".venv" / "Scripts" / "python.exe"
STAGE_RUNNER = Path(os.environ["REAPER_STAGE_RUNNER_SCRIPT"]) if os.environ.get("REAPER_STAGE_RUNNER_SCRIPT") \
    else STAGE_RUNNER_DIR / "stage_runner.py"

# API-Watchdog: bewusst GROSSER als die interne Wall-Clock-Notbremse des Runners
# (20 min), damit eine normale Stage immer zuerst selbst endet (rc=2); feuert nur bei
# echtem Hang und beendet seither den Prozessbaum AKTIV (P4, Job-Object).
# Per Env-Var verkuerzbar NUR fuer Regressionstests (test-queue/test_live_api.py,
# Watchdog-unter-voller-Queue-Fall) -- ohne die Variable unveraendert 25 min.
STAGE_TIMEOUT_S = int(os.environ.get("REAPER_STAGE_TIMEOUT_S", str(25 * 60)))

# Wie lange POST /abort auf die Finalisierung des Kills wartet, bevor es mit 500 antwortet.
# Normal: Kill + Reap + Pipe-Drain-Ende + JSONL-Event dauern deutlich unter einer Sekunde.
ABORT_FINALIZE_TIMEOUT_S = 30

# Pro Pipe so viel Tail in Speicher/Event halten (der Runner schreibt praktisch nur stderr).
TAIL_LIMIT = 8 * 1024

# P5: Herzschlag-Intervall des SSE-Streams (Stage laeuft sonst bis zu 20 min;
# das Event haelt den Stream sichtbar live und beweist Fortschritt an den Client).
HEARTBEAT_INTERVAL_S = 5.0

# Begrenzte SSE-Event-Queue (P1-Fix): bei einem langsamen/getrennten Client UND
# gleichzeitig hohem Logaufkommen (z. B. eine Stage mit vielen Tool-Calls) wuchs
# die vorher unbegrenzte queue.Queue() ohne Obergrenze -- log_line-Events werden
# jetzt bei Ueberlast verworfen (gezaehlt, am Stage-Ende als ein log_dropped-Event
# gemeldet), stage_started/stage_completed/Sentinel werden GARANTIERT zugestellt
# (verdraengen notfalls aeltere log_line-Eintraege). 1000 Eintraege ~ mehrere
# Minuten normaler Log-Rate bei 5-10 Zeilen/s -- grosszuegig genug fuer kurze
# Verzoegerungen, klein genug um den Speicher pro Stage hart zu begrenzen.
SSE_QUEUE_MAXSIZE = 1000

# Request-Defaults: lokal vorhandene Ollama-Modelle (verifiziert 2026-10-15: qwen3:8b
# 8.2B Q4_K_M und qwen3.8-q5 27.3B Q5_K_M; das qwen36-a3b:latest aus dem Runner-Beispiel
# existiert NICHT). Seit dem Umzug weg von Jan (2026-10-16) ist Ollama die EINZIGE
# Modell-Quelle -- qwen3.8-q5 ist Reapers Hauptmodell (role=main, volle Persona),
# qwen3:8b bleibt das schnelle Default-Modell fuer run_substage-Worker-Stages.
DEFAULT_MODEL = "qwen3.8-q5:latest"
DEFAULT_PROJECT = "coding-v2"  # Basic-Memory-Projekt, in dem das Stage-Wissen haengt

# Exit-Code -> Status. NUR hier abgeleitet; Logtext wird NICHT fuer Status geparst.
EXIT_OUTCOMES = {0: "success", 1: "error", 2: "wall_clock", 3: "max_iterations"}

# Status fuer aktiv beendete Stages (kein Runner-Urteil -> exit_code bleibt null):
STATUS_ABORTED = "aborted"            # vom Nutzer ueber POST /abort abgebrochen
STATUS_WATCHDOG = "watchdog_timeout"  # API-Watchdog hat die Stage beendet

# Race-Fix (P1): Sentinel-Wert fuer _kill_intent[session_id] -- markiert den
# LINEARISIERUNGSPUNKT zwischen natuerlichem Prozessende und Abbruch (siehe
# _finalize_stage/_terminate_live_stage). _kill_intent dient damit nicht mehr
# nur als "welcher Abbruchgrund", sondern als EINZIGE verbindliche Entscheidung:
# None = noch offen, _NATURAL = natuerliches Ende hat den Punkt zuerst erreicht
# (ein danach eintreffender Abort kommt zu spaet), jeder andere String = ein
# Abbruchgrund hat den Punkt zuerst erreicht (erster Abbruchgrund gewinnt).
_NATURAL = "__natural_completion__"

CREATE_NO_WINDOW = 0x08000000  # Windows: kein Konsolenfenster (wie der Runner bei pwsh)

_FINISH_MARKER = "=== STAGE BEENDET"
_TS_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ", flags=re.M)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_event(etype: str, data: dict | None = None) -> dict:
    """Ein JSONL-Event: genau ein JSON-Objekt pro Zeile."""
    return {"ts": _utcnow(), "type": etype, "data": data or {}}


def _append_event(session_id: str, event: dict) -> None:
    """Haengt EIN Event an die Session-Datei an (Datei wird nie ueberschrieben)."""
    path = SESSIONS_DIR / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _apply_event(session: dict, event: dict) -> None:
    """Faltet EINES JSONL-Events in den Session-Zustand ein.

    Rekonstruktion = alle Zeilen der Datei in Reihenfolge durch diese Funktion
    laufen lassen; spaetere Events erweitern/ueberschreiben fruhere. Unbekannte
    Typen werden bewusst ignoriert (Reserve fuer spaetere Phasen).
    """
    etype = event.get("type")
    data = event.get("data") or {}
    if etype == "session_created":
        session["created_at"] = event.get("ts") or session.get("created_at")
    elif etype == "message_received":
        session["messages"].append({
            "role": data.get("role"), "content": data.get("content"),
            "stage_id": data.get("stage_id"), "ts": event.get("ts"),
        })
    elif etype == "stage_started":
        session["stages"].append({
            "stage_id": data.get("stage_id"), "status": "running",
            "exit_code": data.get("exit_code"), "pid": data.get("pid"),
            "model": data.get("model"), "project": data.get("project"),
            "started_at": data.get("started_at") or event.get("ts"),
            "finished_at": None, "duration_s": None,
            "summary": None, "stdout_tail": "", "stderr_tail": "",
        })
    elif etype == "stage_completed":
        stage = next((s for s in session["stages"]
                      if s.get("stage_id") == data.get("stage_id")), None)
        if stage is None:
            # Defensiv: Terminal-Event ohne stage_started (darf normal nicht vorkommen).
            log.warning("stage_completed ohne stage_started fuer %s -- selbststaendig gefaltet",
                        data.get("stage_id"))
            stage = {"stage_id": data.get("stage_id"), "status": None, "exit_code": None,
                     "pid": None, "model": data.get("model"), "project": data.get("project"),
                     "started_at": None, "finished_at": None, "duration_s": None,
                     "summary": None, "stdout_tail": "", "stderr_tail": ""}
            session["stages"].append(stage)
        for key in ("status", "exit_code", "finished_at", "duration_s",
                    "summary", "stdout_tail", "stderr_tail"):
            stage[key] = data.get(key)


def _load_sessions() -> dict[str, dict]:
    """Liest beim Serverstart alle Session-Dateien und rekonstruiert den Zustand
    aus der vollen Event-Folge (nicht nur die letzte Zeile)."""
    sessions: dict[str, dict] = {}
    for path in sorted(SESSIONS_DIR.glob("*.jsonl")):
        session: dict = {
            "session_id": path.stem,
            "created_at": None,
            "messages": [],  # role/content/stage_id/ts aus message_received
            "stages": [],    # stage_id/status/... aus stage_started + stage_completed
        }
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s Zeile %d: keine gueltige JSON, Zeile uebersprungen", path.name, lineno)
                continue
            _apply_event(session, event)
        if session["created_at"] is None:
            log.warning("%s: kein session_created-Event, Datei nicht als Session geladen", path.name)
            continue
        for stage in session["stages"]:
            if stage.get("status") == "running":
                # Zielarchitektur: unklar abgeschlossene Stages (z. B. nach API-Crash)
                # werden als unvollstaendig markiert und NICHT automatisch neu ausgefuehrt.
                # (Seit P4 toetet das Job-Object die Baeume bei API-Tod mit; der Status
                # dokumentiert trotzdem, dass kein sauberes Runner-Urteil vorliegt.)
                stage["status"] = "incomplete"
                log.warning("%s: Stage %s war beim Neustart noch 'running' -> 'incomplete'",
                            path.name, stage.get("stage_id"))
        sessions[session["session_id"]] = session
    return sessions


# ---------------------------------------------------------------------------
# P4: Prozessbaum-Kill via Windows-Job-Object (KILL_ON_JOB_CLOSE)
#
# Entscheidung Job-Object statt psutil-Prozessbaum-Kill:
# - atomar auf Kernel-Ebene: TerminateJobObject toetet ALLE Prozesse des Jobs
#   (Hauptprozess + alle pwsh-Kinder/Enkel, auch solche, die ZWISCHEN Ermittlung
#   und Kill gestartet werden). Der psutil-Weg (Kinder rekursiv ermitteln +
#   einzeln terminieren) hat ein Race-Fenster: stage_runner.py kann jederzeit
#   pwsh starten (Tool-Timeout bis 600 s), spaet gestartete Kinder ueberleben dort.
# - keine neue Abhaengigkeit: ctypes ist Stdlib; psutil waere eine neue venv-
#   Abhaengigkeit gegen das KISS-Prinzip der Zielarchitektur.
# - Nebeneffekt: stirbt der API-Prozess selbst (Crash/Ctrl+C), schliessen sich
#   die Job-Handles und das OS beendet die Baeume -- keine Orphan-Prozesse mehr
#   nach einem API-Crash (vor P4 blieb nur die 'incomplete'-Markierung beim
#   Neustart; der verwaiste Prozess lief einfach weiter).
# (Bewusste Annahme: ein privilegiertes Prozess koennte theoretisch aus dem Job
#  "ausbrechen"; fuer ein lokales Single-User-System nicht realistisch.)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # SetInformationJobObject-Klasse (WinAPI-Enum JobObjectExtendedLimitInformation)
    _PROCESS_SET_QUOTA = 0x0100                # fuer AssignProcessToJobObject noetig
    PROCESS_TERMINATE = 0x0001                 # Fallback-Terminierung des Hauptprozesses

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_ulonglong),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE


def _create_kill_job() -> int:
    """Legt ein Job-Object mit KILL_ON_JOB_CLOSE an (Handle schliesst = Baum toetet)."""
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(f"CreateJobObjectW fehlgeschlagen (err={ctypes.get_last_error()})")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(handle, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(f"SetInformationJobObject fehlgeschlagen (err={err})")
    return handle


def _close_job_handle(handle: int) -> None:
    kernel32.CloseHandle(handle)


def _assign_process_to_job(job_handle: int, proc: subprocess.Popen) -> None:
    """Weist den Stage-Prozess dem Kill-Job zu. Alles, was dieser Prozess SPATER
    startet (pwsh-Kinder), gehoert automatisch zum selben Job (Kindprozesse erben
    die Job-Mitgliedschaft) -- damit deckt ein Handle den ganzen Baum ab."""
    if kernel32.AssignProcessToJobObject(job_handle, proc._handle):
        return
    err = ctypes.get_last_error()
    # Fallback: frisches Handle mit den fuer die Zuordnung noetigen Rechten oeffnen
    # (PROCESS_SET_QUOTA); in der Praxis reicht das Popen-eigene Handle.
    h = kernel32.OpenProcess(_PROCESS_SET_QUOTA, False, proc.pid)
    if h and kernel32.AssignProcessToJobObject(job_handle, h):
        kernel32.CloseHandle(h)
        return
    if h:
        kernel32.CloseHandle(h)
    raise OSError(f"AssignProcessToJobObject fehlgeschlagen (err={err})")


class LiveStage:
    """Registry-Eintrag einer laufenden Stage (Schluessel: session_id).

    Der Worker-Thread (post_message) besitzt proc/job_handle und ist der EINZIGE,
    der killt und persistiert (unter Session-Lock, genau einmal). /abort setzt nur
    abort_event und wartet auf done_event -- so bleibt die JSONL-Reihenfolge
    stage_started/stage_completed unveraendert.
    """
    __slots__ = ("stage_id", "proc", "job_handle", "abort_event", "done_event", "job_closed")

    def __init__(self, stage_id: str, proc: subprocess.Popen, job_handle: int) -> None:
        self.stage_id = stage_id
        self.proc = proc
        self.job_handle = job_handle
        self.abort_event = threading.Event()  # /abort -> Worker: bitte beenden
        self.done_event = threading.Event()   # Worker -> /abort: finalisiert + persistiert
        # Race-Fix (P1, Punkt A): schuetzt vor TerminateJobObject() auf einem
        # bereits geschlossenen Handle. NUR unter _live_guard lesen/setzen (siehe
        # _kill_job_locked/_close_job_handle_locked) -- dadurch sind Terminierung
        # und Handle-Schliessung gegenseitig ausgeschlossen, nie gleichzeitig.
        self.job_closed = False


# Laufzeit-Zustand: In-Memory-Index der persistierten Sessions; wird beim Start
# von _load_sessions() befuellt und von den Endpunkten erweitert.
_sessions: dict[str, dict] = {}

# Per-Session-Lock: serialisiert Stages EINER Session (einzelner uvicorn-Prozess,
# FastAPI laeuft synchrone Endpunkte im Threadpool).
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()

# P4: Registry laufender Stages fuer POST /abort (session_id -> LiveStage, maximal
# eine pro Session). Bewusst getrennt vom Session-Lock, weil /abort NICHT hinter
# dem Lock einer laufenden Stage blockieren darf (der Worker haelt ihn bis zum Ende).
_live_stages: dict[str, LiveStage] = {}
_live_guard = threading.Lock()
# P4: Kill-Intent-Registry (session_id -> Status-Wort "aborted"/"watchdog_timeout").
# Von _terminate_live_stage unter _live_guard gesetzt (VOR dem Kill, Rule A)
# und vom Worker bei der Finalisierung gepoppt -- gleiches Lock-Schema wie
# _live_stages. Der Worker liest/loescht den Eintrag immer unter _live_guard.
_kill_intent: dict[str, str] = {}


def _session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def _running_stage(session: dict) -> dict | None:
    return next((s for s in session["stages"] if s.get("status") == "running"), None)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    TASK_TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not STAGE_PYTHON.exists() or not STAGE_RUNNER.exists():
        log.warning("stage_runner.py unvollstaendig (%s / %s) -- POST /messages wird 500 liefern",
                    STAGE_PYTHON, STAGE_RUNNER)
    _sessions.update(_load_sessions())
    if _sessions:
        log.info("%d Session(en) aus %s rekonstruiert", len(_sessions), SESSIONS_DIR)
    yield


# Nur Loopback: lokales Single-User-System, keine Authentifizierung (Architektur-constraint).
app = FastAPI(
    title="Reaper API",
    version="0.6.0-gui",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

WEB_DIR = APP_DIR / "web"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Liefert die einseitige Chat-GUI (web/index.html) aus; spricht NUR die
    oeffentlichen Endpunkte dieser API an (Sessions/Messages/Abort), kennt
    stage_runner.py/Ollama/Basic-Memory nicht."""
    return FileResponse(WEB_DIR / "index.html")


OLLAMA_HEALTH_HOST = "127.0.0.1"
OLLAMA_HEALTH_PORT = 11434


def _tcp_reachable(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


@app.get("/health", include_in_schema=False)
def health() -> dict:
    """Rein diagnostischer Status fuer die GUI-Ampel -- KEINE funktionale
    Abhaengigkeit: main.py ruft Ollama nie selbst auf (das macht ausschliesslich
    der stage_runner.py-Subprozess), hier wird nur per TCP-Connect geprueft, ob
    der Port offen ist, damit die GUI anzeigen kann, warum eine Stage haengen
    wuerde, BEVOR der Nutzer eine Nachricht abschickt."""
    return {
        "api": "ok",
        "ollama": "ok" if _tcp_reachable(OLLAMA_HEALTH_HOST, OLLAMA_HEALTH_PORT) else "down",
        "stage_runner": "ok" if STAGE_PYTHON.exists() and STAGE_RUNNER.exists() else "missing",
    }


class MessageIn(BaseModel):
    """Body von POST /sessions/{id}/messages. model/project/think sind uebersteuerbar;
    Defaults = lokal vorhandenes Ollama-Modell, Standard-Memory-Projekt, Reasoning AUS
    (deterministisch; True = Modell denkt mit -- bei Qwen3/Q5 praktisch unbegrenzt)."""

    message: str = Field(min_length=1, description="Nutzer-Task-Text (= Stage-Auftrag)")
    model: str = Field(default=DEFAULT_MODEL, min_length=1,
                       description=f"Ollama-Modellname (Default {DEFAULT_MODEL})")
    project: str = Field(default=DEFAULT_PROJECT, min_length=1,
                         description=f"Basic-Memory-Projekt (Default {DEFAULT_PROJECT})")
    # think wird IMMER explizit an stage_runner.py durchgereicht (--think on/off):
    # fehlt der Key, laeuft der Runner mit "auto" und das Modell nutzt seinen eigenen
    # Reasoning-Default (Qwen3/Q5: praktisch unbegrenzt) -- E2E-Bug, fix 2026-10-16.
    think: bool = Field(default=False,
                        description="Reasoning/Thinking des Modells (Ollama 'think'); Default aus")

    @field_validator("message")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message darf nicht leer oder nur Leerraum sein")
        return v


class BoundedSseQueue:
    """Begrenzte SSE-Event-Queue mit zwei Zustellklassen:

    - log_line (ueber put_log_line): best-effort, NIE blockierend. Bei voller
      Queue wird die Zeile verworfen und nur gezaehlt (kein queue.put() mit
      unbegrenzter Wartezeit, kein Einzel-Warning pro Zeile -- Anforderung 3/4).
      Der stderr-Drainer (_drain_capped) bleibt dadurch immer lauffaehig, auch
      wenn der SSE-Client nicht konsumiert; die stderr-Pipe wird unabhaengig
      vom Consumer weiter geleert (kein Pipe-Deadlock-Risiko).
    - Prioritaet (ueber put_priority): stage_started/stage_completed/Sentinel
      MUESSEN ankommen -- bei voller Queue wird GEZIELT ein log_line-Eintrag
      verdraengt, NIE ein anderes Prioritaets-Event. Blockiert dabei NIE
      unbegrenzt, da die Queue stets endlich ist und verdraengt statt zu warten.

    KORREKTUR (gefunden durch gezielten Test, siehe test_bounded_queue.py
    test_priority_eviction_never_removes_priority_event): die urspruengliche
    Fassung verdraengte blind den AELTESTEN Eintrag, egal welchen Typs. Da
    stage_started IMMER als erstes Element eingereiht wird (_start_stage, vor
    den Drainer-Threads), ist es bei einer spaeter voll gelaufenen Queue exakt
    der aelteste Eintrag -- ein nachfolgender put_priority(stage_completed)
    haette stage_started dadurch VERDRAENGT statt eines log_line-Eintrags.
    _evict_one_log_line_locked() sucht deshalb gezielt den ersten log_line-
    Eintrag (reiht uebersprungene Nicht-log_line-Eintraege in Original-
    reihenfolge wieder ein) statt blind das Kopfelement zu entfernen.

    Alle mutierenden Operationen (put_log_line, put_priority/Verdraengung,
    Dropped-Zaehler) laufen unter EINEM gemeinsamen Lock, damit die Verdraengung
    (mehrschrittig: Queue durchsuchen, Nicht-Opfer wieder einreihen) atomar
    gegenueber anderen Producern ist -- sonst koennte ein paralleler
    put_log_line()-Aufruf mitten in der Suche einen weiteren Eintrag einfuegen
    und das Ergebnis verfaelschen. get() (Consumer-Seite) bleibt lockfrei,
    queue.Queue ist dafuer bereits intern threadsicher; die Verdraengung ist mit
    maxsize=1000 ein kurzer, begrenzter Scan (Mikrosekunden), kein
    unbegrenztes Warten."""

    __slots__ = ("_q", "_dropped", "_lock")

    def __init__(self, maxsize: int = SSE_QUEUE_MAXSIZE) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._lock = threading.Lock()

    def put_log_line(self, data: dict) -> None:
        with self._lock:
            try:
                self._q.put_nowait(("log_line", data))
            except queue.Full:
                self._dropped += 1

    def put_priority(self, item: tuple[str, dict] | None) -> None:
        with self._lock:
            while True:
                try:
                    self._q.put_nowait(item)
                    return
                except queue.Full:
                    if not self._evict_one_log_line_locked():
                        # Praktisch unerreichbar: pro Stage gibt es hoechstens 4
                        # Prioritaets-Events (stage_started, log_dropped,
                        # stage_completed, Sentinel) bei maxsize=1000 -- es gibt
                        # also immer mindestens einen log_line-Eintrag zum
                        # Verdraengen. Letzter Ausweg (z. B. bei absichtlich
                        # winziger maxsize in einem Test), damit put_priority()
                        # NIE blockiert: aeltesten Eintrag verdraengen.
                        try:
                            self._q.get_nowait()
                        except queue.Empty:
                            pass

    def _evict_one_log_line_locked(self) -> bool:
        """Aufrufer haelt bereits self._lock. Sucht vom Kopf der Queue aus den
        ERSTEN log_line-Eintrag und entfernt NUR diesen; alle dabei
        uebersprungenen Nicht-log_line-Eintraege (stage_started etc.) werden in
        urspruenglicher Reihenfolge wieder eingereiht. True, wenn ein
        log_line-Eintrag entfernt wurde (Platz gemacht), sonst False."""
        held: list = []
        found = False
        while True:
            try:
                old = self._q.get_nowait()
            except queue.Empty:
                break
            if not found and old is not None and old[0] == "log_line":
                found = True
                self._dropped += 1
                continue  # NICHT wieder einreihen -- das macht den Platz frei
            held.append(old)
        for h in held:
            self._q.put_nowait(h)  # passt immer wieder rein -- wir haben max. 1 entfernt
        return found

    def pop_dropped(self) -> int:
        """Liefert die Anzahl seit dem letzten Aufruf verworfener log_line-Events
        und setzt den Zaehler zurueck -- fuer das optionale log_dropped-Event."""
        with self._lock:
            n = self._dropped
            self._dropped = 0
            return n

    def get(self, timeout: float):
        """Wie queue.Queue.get(timeout=...): blockiert bis Item oder wirft
        queue.Empty nach Ablauf -- unveraendertes Consumer-Verhalten."""
        return self._q.get(timeout=timeout)


def _drain_capped(stream, chunks: list[bytes], limit: int,
                   events: "BoundedSseQueue | None" = None) -> None:
    """Leert eine Pipe DURCHGAENGIG waehrend der Subprozess laeuft (voller Puffer =
    Deadlock) und haelt nur die letzten `limit` Bytes (Tail) fuer Event/Response).
    Mit `events`: pusht zusaetzlich JEDE vollstaendige Zeile SOFORT als eigenes
    SSE-Event ('log_line') -- live-Sichtbarkeit in der GUI, z. B. ob/wann
    run_substage laeuft, ohne auf das Stage-Ende zu warten. Bewusst nur fuer
    stderr genutzt (stage_runner.py._log() schreibt dorthin, stdout ist leer)."""
    try:
        total = 0
        buf = b""
        while True:
            # read1() statt read(): read() auf einem BufferedReader versucht, die
            # VOLLEN 65536 Bytes zu sammeln (blockiert also, bis entweder so viel
            # Output anfiel oder die Pipe schloss) -- fuer Live-Streaming unbrauchbar,
            # eine Stage mit wenig Output haette NIE eine Zeile live gezeigt, bis sie
            # fertig war. read1() liefert sofort, was verfuegbar ist (mind. 1 Byte).
            chunk = stream.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            while total > limit and len(chunks) > 1:
                total -= len(chunks.pop(0))
            if events is not None:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").rstrip("\r")
                    if text:
                        events.put_log_line({"text": text})
    except Exception as exc:
        log.warning("Pipe-Drain unterbrochen: %s", exc)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _delete_quiet(path: str | None) -> None:
    """Temp-Taskdatei loeschen -- Erfolg UND Fehlerfall (P3-Pflicht)."""
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Temp-Task-Datei %s nicht loeschbar: %s", path, exc)


def _extract_summary(rc: int, stderr_tail: str) -> str:
    """finish_stage-Zusammenfassung als Anzeigetext aus dem Runner-Log (stderr-Tail).

    Der STATUS kommt NIE aus Logtext (immer Exit-Code); hier wird nur der Text nach
    dem '=== STAGE BEENDET'-Marker als Anzeige uebernommen (Zielarchitektur v1).
    """
    idx = stderr_tail.find(_FINISH_MARKER)
    if idx != -1:
        block = stderr_tail[idx + len(_FINISH_MARKER):]
        nl = block.find("\n")  # Rest der Markerzeile "(success=...)" verwerfen
        if nl != -1:
            block = block[nl + 1:]
        text = _TS_PREFIX.sub("", block).strip()
        if text:
            return text
    if rc == 0:
        return "(finish_stage-Zusammenfassung nicht im stderr-Tail gefunden)"
    fallback = {
        1: "Runner-Fehler oder success=false (Details in stderr_tail).",
        2: "Abgebrochen: Wall-Clock-Limit (20 min) erreicht, kein finish_stage-Aufruf.",
        3: "Abgebrochen: maximale Iterationszahl erreicht, kein finish_stage-Aufruf.",
    }
    return fallback.get(rc, f"Unbekannter Exit-Code {rc} (Details in stderr_tail).")

# ---------------------------------------------------------------------------
# Endpunkte + Stage-Worker (P3-Logik; P4: Kill-Job pro Stage, abort_event,
# aktiver Watchdog, POST /abort)
# ---------------------------------------------------------------------------


def _kill_job(job_handle: int) -> None:
    """TerminateJobObject auf dem gehaltenen Job-Handle: toetet ALLE Prozesse
    des Jobs atomar (Hauptprozess + alle Kindprozesse inkl. pwsh-Stages).
    Nur der /abort-Handler und der Watchdog rufen das -- nie der Worker."""
    if not kernel32.TerminateJobObject(job_handle, 1):
        log.warning("TerminateJobObject fehlgeschlagen (err=%d) -- Baum laeuft ggf. weiter",
                    ctypes.get_last_error())


def _kill_job_locked(live: "LiveStage") -> None:
    """Race-Fix (P1, Punkt A): Aufrufer MUSS bereits _live_guard halten. Ruft
    TerminateJobObject nur auf, wenn das Handle noch nicht geschlossen wurde --
    verhindert, dass ein Kill auf ein bereits (von _finalize_stage) geschlossenes
    Handle trifft (Use-after-close/Handle-Reuse). Da sowohl diese Funktion als
    auch _close_job_handle_locked() ausschliesslich unter _live_guard laufen,
    sind Terminierung und Schliessen des Handles gegenseitig ausgeschlossen."""
    if live.job_closed:
        return
    _kill_job(live.job_handle)


def _close_job_handle_locked(live: "LiveStage") -> None:
    """Race-Fix (P1, Punkt A): Aufrufer MUSS bereits _live_guard halten. Schliesst
    das Handle hoechstens einmal (job_closed-Flag) -- danach kann _kill_job_locked
    nie wieder auf dieses Handle zugreifen, egal welcher Thread als naechstes
    _live_guard bekommt."""
    if live.job_closed:
        return
    _close_job_handle(live.job_handle)
    live.job_closed = True


def _terminate_live_stage(session_id: str, status: str) -> LiveStage | None:
    """Der EINZIGE Weg, eine laufende Stage zu toeten -- POST /abort UND
    Watchdog nutzen exakt diesen Mechanismus. Liefert die LiveStage oder None,
    wenn keine aktive Stage existiert ODER das natuerliche Ende den
    Linearisierungspunkt bereits zuerst erreicht hat (Punkt C, zu spaet).

    Race-Fix (P1): kill-intent setzen, abort_event setzen UND der eigentliche
    Kill (_kill_job_locked) laufen jetzt alle unter DEMSELBEN _live_guard-Aufruf
    -- vorher lag der Kill ausserhalb des Locks und konnte mit einem parallelen
    _close_job_handle() in _finalize_stage um das Handle wettlaufen (Punkt A,
    Use-after-close/Handle-Reuse). _kill_intent ist jetzt die EINZIGE
    verbindliche Entscheidung (siehe _NATURAL/_finalize_stage):
    - bereits _NATURAL: natuerliches Ende hat den Linearisierungspunkt zuerst
      erreicht -- Abort kommt zu spaet, Ergebnis bleibt unveraendert (Punkt C).
      Kein Kill noetig, der Prozess ist bereits (per _wait_proc bestaetigt) tot.
    - bereits ein anderer Status: ein frueherer Abbruchgrund hat gewonnen
      (Punkt B, erster gewinnt) -- wir ueberschreiben ihn NICHT, setzen
      abort_event/Kill aber trotzdem erneut (idempotent, schadet nicht, falls
      der erste Versuch den Prozess noch nicht erreicht hat).
    - None (offen): wir sind die ersten -- unser `status` gewinnt verbindlich."""
    with _live_guard:
        live = _live_stages.get(session_id)
        if live is None:
            return None
        existing = _kill_intent.get(session_id)
        if existing == _NATURAL:
            return None
        if existing is None:
            _kill_intent[session_id] = status
        live.abort_event.set()
        _kill_job_locked(live)
    return live


def _watchdog_thread(session_id: str, live: LiveStage, start_mono: float) -> None:
    """Pro-Stage-Watchdog (P4): waecht exakt nach STAGE_TIMEOUT_S auf -- oder
    frueher, wenn der Worker die Stage fertig finalisiert hat (done_event wird
    am Ende gesetzt) -- und toetet dann den Prozessbaum AKTIV statt die Stage
    laufen zu lassen (vor P4: nur Status='incomplete' gesetzt). Normal feuert
    der Runner zuerst selbst (20-min-Notbremse, rc=2); hier endet nur ein
    echter Hang."""
    remaining = STAGE_TIMEOUT_S - (time.monotonic() - start_mono)
    if remaining > 0:
        live.done_event.wait(timeout=remaining)
    if live.done_event.is_set():
        return  # Stage normal fertig -- nichts zu tun
    log.warning("Watchdog: Stage %s (Session %s) uebersteigt %.0f s -- Job killen",
                live.stage_id, session_id, STAGE_TIMEOUT_S)
    term = _terminate_live_stage(session_id, STATUS_WATCHDOG)
    if term is not None:
        term.done_event.wait(timeout=ABORT_FINALIZE_TIMEOUT_S)


def _wait_proc(proc: subprocess.Popen, session_id: str, deadline: float) -> int | None:
    """proc.poll() in einer 1-s-Schleife statt blockierendem wait (P4-Spec): der
    Worker beobachtet das Ende ohne unbegrenzte Blockade. WICHTIG: poll(),
    NICHT wait(timeout) -- wait(timeout) RAEST TimeoutExpired, solange der
    Prozess laeuft (er liefert nie None), und haette jede Stage sofort zum
    Crash gebracht. Liefert den Exit-Code; ist die Deadline ueberschritten,
    toetet der Worker EINMALIG selbst via _terminate_live_stage (gleicher
    Mechanismus wie /abort) -- nur Backstop fuer den Fall, dass der Watchdog
    aus einem Grund nicht da ist."""
    backstop_done = False
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        if time.monotonic() >= deadline and not backstop_done:
            log.error("Stage (Session %s) ueber Deadline, Watchdog noch nicht da -- "
                      "Backstop-Kill vom Worker", session_id)
            _terminate_live_stage(session_id, STATUS_WATCHDOG)
            backstop_done = True
        time.sleep(1.0)


@app.post("/sessions", status_code=201)
def create_session() -> dict:
    """Neue Chat-Session anlegen (P3, Logik unveraendert). Das
    session_created-Event ist die Persistenzbasis der Rekonstruktion."""
    session_id = str(uuid.uuid4())
    session = {"session_id": session_id, "created_at": _utcnow(),
               "messages": [], "stages": []}
    _append_event(session_id, _new_event("session_created"))
    _sessions[session_id] = session
    return session


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    """Session lesen (P3, Logik unveraendert); 404 bei unbekannter id."""
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return session


@app.get("/sessions/{session_id}/context", include_in_schema=False)
def get_context_preview(session_id: str) -> dict:
    """Zeigt, was der begrenzte Kontext (_build_task_text) JETZT einer naechsten
    Nachricht voranstellen wuerde -- reine Vorschau, startet keine Stage. Fuer
    das Kontext-Panel der GUI."""
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    turns = _collect_context_turns(session)
    return {
        "turns_included": len(turns),
        "max_turns": CONTEXT_MAX_TURNS,
        "max_chars": CONTEXT_MAX_CHARS,
        "preview": _render_context_history(turns),
    }


@dataclass
class StageContext:
    """Zwischenstand einer Stage zwischen Startphase (Request-Thread, unter
    Session-Lock) und Finalisierungsphase (Worker-Thread, ohne Lock)."""

    session_id: str
    body: MessageIn
    stage_id: str
    proc: subprocess.Popen
    job_handle: int
    live: LiveStage
    task_path: str
    start_mono: float
    deadline: float
    started_at: str
    out_chunks: list[bytes] = field(default_factory=list)
    err_chunks: list[bytes] = field(default_factory=list)
    drainers: list[threading.Thread] = field(default_factory=list)


def _sse_frame(etype: str, data: dict) -> str:
    """Standard-SSE-Rahmen: 'event: <typ>\ndata: <eine JSON-Zeile>\n\n'."""
    return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# Folgepaket "begrenzter Kontext fuer Folgeauftraege" (Zielarchitektur v1, bewusst
# NICHT Teil von P5): bewusst KEIN Vollverlauf -- nur eine kurze Orientierung ueber
# die letzten paar abgeschlossenen Turns, damit ein Folgeauftrag nicht komplett
# kontextlos ist. Dauerhaftes Wissen bleibt weiterhin Aufgabe des Modells selbst
# (memory_write), das hier NICHT ersetzt.
CONTEXT_MAX_TURNS = 5
CONTEXT_MAX_CHARS = 6000


def _collect_context_turns(session: dict) -> list[tuple[str, str]]:
    """Die letzten CONTEXT_MAX_TURNS ABGESCHLOSSENEN Turns dieser Session
    (Nutzer-Nachricht, Stage-Zusammenfassung). Keine laufenden Stages (koennen
    hier gar nicht vorkommen, da ein Start erst nach der 409-Pruefung erfolgt)."""
    stages_by_id = {s["stage_id"]: s for s in session.get("stages", [])}
    turns: list[tuple[str, str]] = []
    for m in session.get("messages", []):
        stage = stages_by_id.get(m.get("stage_id"))
        if stage is None or stage.get("status") in (None, "running"):
            continue
        summary = stage.get("summary") or f"(Status: {stage.get('status')}, keine Zusammenfassung)"
        turns.append((m.get("content", ""), summary))
    return turns[-CONTEXT_MAX_TURNS:]


def _render_context_history(turns: list[tuple[str, str]]) -> str:
    """Rendert die Turns als Orientierungstext, bei CONTEXT_MAX_CHARS gekappt.
    Leerer String, wenn keine Turns vorliegen (erster Auftrag einer Session)."""
    if not turns:
        return ""
    lines = ["Bisheriger Verlauf dieser Session (kurze Orientierung, nicht zwingend "
             "alles relevant fuer den aktuellen Auftrag):", ""]
    for user_msg, summary in turns:
        lines.append(f"Nutzer (vorher): {user_msg}")
        lines.append(f"Ergebnis: {summary}")
        lines.append("")
    history_text = "\n".join(lines)
    if len(history_text) > CONTEXT_MAX_CHARS:
        history_text = "[...gekuerzt...]\n" + history_text[-CONTEXT_MAX_CHARS:]
    return history_text


def _build_task_text(session: dict, message: str) -> str:
    """Baut den Task-Text aus dem begrenzten Kontext (siehe _collect_context_turns/
    _render_context_history) plus der aktuellen Nachricht. Wird NUR in den
    Task-Slot geschrieben (was das Modell sieht) -- die persistierte
    message_received-/session["messages"]-Historie bleibt die ORIGINALE, kurze
    Nachricht, sonst wuerde sich der Kontext bei jedem Folge-Turn selbst-
    rekursiv aufblaehen."""
    history_text = _render_context_history(_collect_context_turns(session))
    if not history_text:
        return message
    return f"{history_text}--- Aktueller Auftrag ---\n{message}"


def _start_stage(session_id: str, body: MessageIn,
                  events: BoundedSseQueue) -> tuple[StageContext, dict]:
    """P5-Startphase -- die bisherige _run_stage-Logik BIS VOR die Warte- und
    Finalisierungsphase, 1:1 unveraendert: Task-Tempdatei, Kill-Job, Subprozess,
    LiveStage-Registry, stage_record + JSONL-Events (message_received,
    stage_started), Pipe-Drainer, Watchdog. Wird vom POST /messages-Handler
    UNTER dem Session-Lock aufgerufen (404/409-Pruefung vorher). Liefert den
    Kontext fuer die Finalisierungsphase plus das stage_started-Datenobjekt
    (wird als erstes SSE-Event gepusht). `events`: der stderr-Drainer pusht
    jede Log-Zeile sofort als 'log_line'-Event (Live-Sichtbarkeit, z. B. ob/
    wann run_substage laeuft -- Nutzer-Anfrage nach einem unbemerkten Haenger)."""
    stage_id = str(uuid.uuid4())
    started_at = _utcnow()
    start_mono = time.monotonic()
    deadline = start_mono + STAGE_TIMEOUT_S
    session = _sessions[session_id]

    # Task-Text als UTF-8-Tempdatei: --task @datei umgeht das Windows-argv-Limit (P3).
    # Folgepaket "begrenzter Kontext": der Task-Slot bekommt nicht mehr NUR die
    # aktuelle Nachricht, sondern zusaetzlich die letzten CONTEXT_MAX_TURNS
    # abgeschlossenen Turns dieser Session als kurze Orientierung (_build_task_text).
    fd, task_path = tempfile.mkstemp(prefix=f"{stage_id}-", suffix=".txt", dir=TASK_TMP_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_build_task_text(session, body.message))

    # P4: erst Job anlegen, dann Prozess starten, dann zuweisen. KILL_ON_JOB_CLOSE
    # ist ab SetInformationJobObject aktiv; alles, was der Prozess spaeter startet
    # (pwsh-Kinder), erbt die Job-Mitgliedschaft -> ein Handle deckt den ganzen Baum.
    job_handle: int | None = None
    proc: subprocess.Popen | None = None
    try:
        job_handle = _create_kill_job()
        proc = subprocess.Popen(
            [str(STAGE_PYTHON), str(STAGE_RUNNER),
             "--model", body.model, "--project", body.project,
             "--think", "on" if body.think else "off",
             "--role", "main",
             "--task", f"@{task_path}"],
            cwd=str(STAGE_RUNNER_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            # Ohne PYTHONIOENCODING schreibt Python auf einem Nicht-Konsolen-Stream
            # unter Windows in der System-Codepage (hier 850, nicht UTF-8) -- main.py
            # dekodiert stdout/stderr hart als UTF-8, also wurden Umlaute etc. zu
            # "?" (gefunden: "Kl?rung" statt "Klaerung" im stderr_tail der GUI).
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        _assign_process_to_job(job_handle, proc)
    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()  # falls Zuweisung fehlte: Hauptprozess nicht hängen lassen
            except OSError:
                pass
        if job_handle is not None:
            _close_job_handle(job_handle)  # toetet verbliebene Baum-Mitglieder mit
        _delete_quiet(task_path)
        log.error("Stage-Start fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail="Stage-Subprozess konnte nicht gestartet werden")

    live = LiveStage(stage_id, proc, job_handle)
    with _live_guard:
        _live_stages[session_id] = live

    stage_record = {
        "stage_id": stage_id, "status": "running", "exit_code": None,
        "pid": proc.pid, "model": body.model, "project": body.project,
        "started_at": started_at, "finished_at": None, "duration_s": None,
        "summary": None, "stdout_tail": "", "stderr_tail": "",
    }
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    drainers: list[threading.Thread] = []
    log.info("Stage %s gestartet (Session %s, pid=%d, model=%s)",
             stage_id, session_id, proc.pid, body.model)
    try:
        session["stages"].append(stage_record)
        msg_event = _new_event("message_received", {
            "role": "user", "content": body.message, "stage_id": stage_id})
        _append_event(session_id, msg_event)
        # Im-Memory-Zustand des LAUFENDEN Servers muss dieselbe Nachricht sehen wie eine
        # Rekonstruktion nach Neustart (_apply_event) -- sonst zeigt GET /sessions/{id}
        # waehrend der Laufzeit leere "messages", obwohl die JSONL-Datei korrekt ist
        # (Bug gefunden beim GUI-Test: Reload zeigte leeren Verlauf trotz korrekter Datei).
        session["messages"].append({
            "role": "user", "content": body.message,
            "stage_id": stage_id, "ts": msg_event["ts"]})
        _append_event(session_id, _new_event("stage_started", {
            "stage_id": stage_id, "exit_code": None, "pid": proc.pid,
            "model": body.model, "project": body.project, "started_at": started_at}))

        # SSE stage_started VOR den Drainern pushen -- garantiert, dass der Client
        # dieses Event vor jeder moeglichen log_line-Zeile sieht (die Drainer
        # koennten sonst sofort nach dem Start schon etwas lesen).
        started_data = {
            "stage_id": stage_id, "status": "running", "exit_code": None,
            "pid": proc.pid, "model": body.model, "project": body.project,
            "started_at": started_at,
        }
        events.put_priority(("stage_started", started_data))

        # stdout/stderr DURCHGAENGIG leeren (voller Pipe-Puffer = Deadlock), Tails behalten.
        drainers = [
            threading.Thread(target=_drain_capped, args=(proc.stdout, out_chunks, TAIL_LIMIT), daemon=True),
            threading.Thread(target=_drain_capped, args=(proc.stderr, err_chunks, TAIL_LIMIT, events), daemon=True),
        ]
        for t in drainers:
            t.start()

        # P4: Watchdog-Thread -- toetet nach STAGE_TIMEOUT_S aktiv den Job.
        watchdog = threading.Thread(
            target=_watchdog_thread, args=(session_id, live, start_mono), daemon=True)
        watchdog.start()
    except Exception:
        # Startphase unvollstaendig (extrem unwahrscheinlich): Gleiche Aufraeum- und
        # Persistenzregeln wie die Fehlerklappe der Finalisierungsphase (Race-Fix
        # P1: _kill_intent-basierte Entscheidung statt nur abort_event, Handle-
        # Schliessen unter demselben Lock wie ein moeglicher Kill-Versuch).
        log.exception("Startphase der Stage %s (Session %s) fehlgeschlagen",
                      stage_id, session_id)
        with _live_guard:
            existing = _kill_intent.get(session_id)
            fail_status = existing if existing not in (None, _NATURAL) else \
                (STATUS_ABORTED if live.abort_event.is_set() else "incomplete")
            _close_job_handle_locked(live)
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)
        stage_record.update({
            "status": fail_status, "exit_code": None, "finished_at": _utcnow(),
            "duration_s": round(time.monotonic() - start_mono, 1),
            "summary": "(API-Fehler waehrend der Startphase)",
            "stdout_tail": "", "stderr_tail": "",
        })
        try:
            _append_event(session_id, _new_event("stage_completed", {
                "stage_id": stage_id, "status": fail_status, "exit_code": None,
                "finished_at": stage_record["finished_at"],
                "duration_s": stage_record["duration_s"],
                "summary": stage_record["summary"],
                "stdout_tail": "", "stderr_tail": ""}))
        except Exception:
            log.exception("stage_completed nach Startfehler nicht persistierbar (Session %s)",
                          session_id)
        _delete_quiet(task_path)
        live.done_event.set()
        raise

    return StageContext(
        session_id=session_id, body=body, stage_id=stage_id, proc=proc,
        job_handle=job_handle, live=live, task_path=task_path,
        start_mono=start_mono, deadline=deadline, started_at=started_at,
        out_chunks=out_chunks, err_chunks=err_chunks, drainers=drainers,
    ), started_data


def _finalize_stage(ctx: StageContext) -> dict:
    """P5-Finalisierungsphase -- die bisherige _run_stage-Logik AB der Warte- und
    Finalisierungsphase, 1:1 unveraendert (Exit-Codes 0-3 sind die einzige
    Statusquelle; Rule A fuer Abbruch; Persistenz des stage_completed-Events;
    Registry-Aufräumung; Job-Handle-Schliessen; done_event). Laeuft im eigenen
    Worker-Thread OHNE Session-Lock. Liefert das volle Stage-Objekt (wird als
    Terminal-SSE-Event an den Client gepusht)."""
    session_id = ctx.session_id
    stage_id = ctx.stage_id
    proc = ctx.proc
    live = ctx.live
    body = ctx.body
    start_mono = ctx.start_mono
    deadline = ctx.deadline
    task_path = ctx.task_path
    out_chunks = ctx.out_chunks
    err_chunks = ctx.err_chunks
    drainers = ctx.drainers

    session = _sessions[session_id]
    stage_record = next(s for s in session["stages"] if s.get("stage_id") == stage_id)
    try:
        rc = _wait_proc(proc, session_id, deadline)

        # LINEARISIERUNGSPUNKT (Punkt C): rc ist jetzt bekannt -- der Prozess hat
        # sich beendet, so oder so. Die Frage "hat vorher schon jemand einen
        # Abbruch registriert" wird HIER, unter _live_guard, EIN EINZIGES MAL
        # verbindlich entschieden -- derselbe Lock, den auch _terminate_live_stage
        # fuer seine Pruefung/Registrierung verwendet, macht die beiden Seiten
        # gegenseitig exklusiv: wer zuerst den Lock bekommt, gewinnt.
        # - _kill_intent noch leer (None): WIR sind zuerst da -- natuerliches
        #   Ende gewinnt verbindlich, mit _NATURAL markieren, damit ein jetzt
        #   noch eintreffender Abort zu spaet ist und nichts mehr aendert.
        # - _kill_intent traegt schon einen Abbruchgrund: der wurde VOR diesem
        #   Punkt registriert -- Rule A, Abbruch gewinnt (status uebernehmen).
        # _kill_intent wird hier bewusst NOCH NICHT entfernt (siehe unten) --
        # sonst koennte ein Abort, der genau in diesem Moment durchkommt, faelsch-
        # lich glauben, er sei der erste, weil der Registry-Eintrag (_live_stages)
        # erst spaeter (nach der Persistenz) entfernt wird.
        with _live_guard:
            existing = _kill_intent.get(session_id)
            if existing is None:
                _kill_intent[session_id] = _NATURAL
                aborted = False
                status = None
            else:
                aborted = existing != _NATURAL  # _NATURAL kann hier nie vorkommen, defensiv
                status = existing if aborted else None

        for t in drainers:
            t.join(timeout=5)
        stdout_tail = b"".join(out_chunks).decode("utf-8", errors="replace")
        stderr_tail = b"".join(err_chunks).decode("utf-8", errors="replace")
        _delete_quiet(task_path)

        finished_at = _utcnow()
        duration_s = round(time.monotonic() - start_mono, 1)

        if aborted:
            # Rule A: Abbruch-Wunsch gewinnt; exit_code bleibt null (kein Runner-Urteil).
            status = status or STATUS_ABORTED
            exit_code = None
            summary = {
                STATUS_ABORTED: "Stage vom Nutzer abgebrochen (POST /abort), kein Runner-Urteil.",
                STATUS_WATCHDOG: f"API-Watchdog beendet die Stage nach {STAGE_TIMEOUT_S // 60} min, kein Runner-Urteil.",
            }.get(status, "Stage aktiv beendet, kein Runner-Urteil.")
        else:
            exit_code = rc
            status = EXIT_OUTCOMES.get(rc, f"unknown_rc_{rc}")
            summary = _extract_summary(rc, stderr_tail)

        stage_record.update({
            "status": status, "exit_code": exit_code, "finished_at": finished_at,
            "duration_s": duration_s, "summary": summary,
            "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
        })
        _append_event(session_id, _new_event("stage_completed", {
            "stage_id": stage_id, "status": status, "exit_code": exit_code,
            "finished_at": finished_at, "duration_s": duration_s, "summary": summary,
            "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
        }))

        with _live_guard:
            # Race-Fix (P1, Punkt A): Handle-Schliessen laeuft jetzt unter
            # DEMSELBEN Lock wie jeder moegliche Kill-Versuch (_kill_job_locked)
            # -- job_closed-Flag macht ein spaeteres TerminateJobObject auf
            # diesem Handle unmoeglich, egal welcher Thread als naechstes dran ist.
            _close_job_handle_locked(live)
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)  # jetzt erst: _NATURAL oder Abbruchgrund entfernen
        live.done_event.set()
        log.info("Stage %s fertig: status=%s rc=%s nach %.1f s",
                 stage_id, status, exit_code, duration_s)
        return {"session_id": session_id, "stage": stage_record}
    except Exception:
        log.exception("Finalisierung der Stage %s (Session %s) fehlgeschlagen",
                      stage_id, session_id)
        with _live_guard:
            existing = _kill_intent.get(session_id)
            fail_status = existing if existing not in (None, _NATURAL) else \
                (STATUS_ABORTED if live.abort_event.is_set() else "incomplete")
            _close_job_handle_locked(live)
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)
        try:
            fail_out = b"".join(out_chunks).decode("utf-8", errors="replace")
            fail_err = b"".join(err_chunks).decode("utf-8", errors="replace")
        except Exception:
            fail_out = fail_err = ""
        stage_record.update({
            "status": fail_status, "exit_code": None, "finished_at": _utcnow(),
            "duration_s": round(time.monotonic() - start_mono, 1),
            "summary": "(API-Fehler waehrend der Finalisierung)",
            "stdout_tail": fail_out, "stderr_tail": fail_err,
        })
        try:
            _append_event(session_id, _new_event("stage_completed", {
                "stage_id": stage_id, "status": fail_status, "exit_code": None,
                "finished_at": stage_record["finished_at"],
                "duration_s": stage_record["duration_s"],
                "summary": stage_record["summary"],
                "stdout_tail": fail_out, "stderr_tail": fail_err}))
        except Exception:
            log.exception("stage_completed nach Fehler nicht persistierbar (Session %s)", session_id)
        _delete_quiet(task_path)
        live.done_event.set()
        raise


def _stage_worker(ctx: StageContext, events: BoundedSseQueue) -> None:
    """P5-Worker-Thread (einer pro POST /messages): finalisiert die in der
    Startphase gestartete Stage und liefert das Ergebnis als Queue-Items an den
    SSE-Generator -- genau EIN Terminal-Event, danach der Sentinel. Laeuft OHNE
    Session-Lock; eine Client-Trennung aendert nichts am Runner-Lauf (die Stage
    laeuft weiter, Finalisierung/Persistenz unveraendert -- Abbruch bleibt
    POST /abort). Terminal-Event und Sentinel sind Prioritaets-Puts (BoundedSseQueue)
    -- kommen garantiert an, auch wenn die Queue zwischenzeitlich voller log_line-
    Events war (die werden dafuer notfalls verdraengt)."""
    try:
        result = _finalize_stage(ctx)
    except Exception:
        # _finalize_stage hat Registry/Job/done_event bereits aufgeraeumt und das
        # stage_completed-Event persistiert; das Stage-Objekt im Session-Zustand
        # spiegelt den Endzustand -- als Terminal-Event liefern.
        log.exception("Stage-Worker: unvorhergesehener Fehler bei Stage %s", ctx.stage_id)
        result = {
            "session_id": ctx.session_id,
            "stage": next((s for s in _sessions[ctx.session_id]["stages"]
                           if s.get("stage_id") == ctx.stage_id), None) or {},
        }
    # Verworfene log_line-Events (Ueberlast) VOR dem Terminal-Event melden -- ein
    # kompaktes Sammel-Event statt einer Warnung pro Zeile; rein informativ ueber
    # verlorene LIVE-Sicht, niemals ueber das (immer vollstaendige) Stage-Ergebnis.
    dropped = events.pop_dropped()
    if dropped:
        events.put_priority(("log_dropped", {"count": dropped}))
    events.put_priority(("stage_completed", result["stage"]))
    events.put_priority(None)  # Sentinel: Stream-Ende


async def _sse_stream(events: BoundedSseQueue, started_mono: float) -> AsyncIterator[str]:
    """P5-Consumer-Seite des SSE-Streams: gibt die gepushten Events im
    Standard-SSE-Format aus (event/data/Rahmenende) und sendet, solange KEIN
    Event kommt (Stage laeuft), alle HEARTBEAT_INTERVAL_S ein heartbeat-Event.
    Beendet sich mit dem Sentinel, der nach genau EINEM Terminal-Event gesetzt
    wird. Client-Trennung waehrend der Stage aendert nichts am Runner-Lauf
    (bewusst: Abbruch bleibt POST /abort); der Generator wird dann einfach nicht
    mehr weiter konsumiert."""
    while True:
        try:
            item = await asyncio.to_thread(events.get, timeout=HEARTBEAT_INTERVAL_S)
        except queue.Empty:
            yield _sse_frame("heartbeat", {
                "elapsed_s": round(time.monotonic() - started_mono, 1)})
            continue
        if item is None:  # Sentinel nach dem Terminal-Event
            break
        etype, data = item
        yield _sse_frame(etype, data)


@app.post("/sessions/{session_id}/messages")
def post_message(session_id: str, body: MessageIn) -> StreamingResponse:
    """Fuehrt die Stage fuer den Nutzer-Task aus und liefert den Fortschritt als
    SSE-Stream (P5): Content-Type text/event-stream, sofort ein stage_started-
    Event, alle 5 s ein heartbeat-Event, am Ende genau EIN Terminal-Event
    stage_completed mit dem vollen Stage-Objekt (status/exit_code/finished_at/
    duration_s/summary/stdout_tail/stderr_tail). Kein Token-Streaming, keine
    Tool-Events (bewusst vertagt, Zielarchitektur v1).

    Die 404/409-Pruefung bleibt VOR dem Stream-Start (Fast Path + Recheck unter
    Session-Lock, unveraendert); auch die Startphase laeuft unter dem Lock
    synchron, sodass ein konkurrierender POST niemals zwei Stages derselben
    Session starten kann. Danach wartet/finalisiert ein Worker-Thread; die
    Antwort wird sofort als StreamingResponse zurueckgegeben."""
    lock = _session_lock(session_id)
    session = _sessions.get(session_id)
    if session is not None and _running_stage(session) is not None:
        raise HTTPException(status_code=409, detail="stage already running for this session")
    events = BoundedSseQueue()
    with lock:
        if _sessions.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if _running_stage(_sessions[session_id]) is not None:
            raise HTTPException(status_code=409, detail="stage already running for this session")
        ctx, _started_data = _start_stage(session_id, body, events)
    # stage_started wird bereits INNERHALB _start_stage gepusht (vor den
    # Drainer-Threads, siehe dort) -- garantiert Reihenfolge vor jeder log_line.
    worker = threading.Thread(target=_stage_worker, args=(ctx, events), daemon=True)
    worker.start()
    return StreamingResponse(
        _sse_stream(events, ctx.start_mono),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



@app.post("/sessions/{session_id}/abort")
def abort_session(session_id: str) -> dict:
    """Bricht die laufende Stage kontrolliert ab (P4): kill-intent + abort_event
    setzen, Job-Object toeten (ganzer Prozessbaum), auf die Worker-Finalisierung
    warten (done_event, max. ABORT_FINALIZE_TIMEOUT_S). Unbekannte Session -> 404;
    bekannte Session OHNE laufende Stage -> 409 (Begrundung: Session-Existenz ist
    die Pruefung mit eigener 404; 'keine aktive Stage' ist ein Zustandskonflikt
    des Requests, kein fehlendes Resource -- wie der 409 beim doppelten POST)."""
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    live = _terminate_live_stage(session_id, STATUS_ABORTED)
    if live is None:
        raise HTTPException(status_code=409, detail="no running stage for this session")
    if not live.done_event.wait(timeout=ABORT_FINALIZE_TIMEOUT_S):
        raise HTTPException(status_code=500, detail="Stage finalisiert nicht nach dem Kill")
    stage = next((s for s in session["stages"] if s.get("stage_id") == live.stage_id), None)
    return {"session_id": session_id, "stage": stage}
