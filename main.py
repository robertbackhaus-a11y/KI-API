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
STAGE_RUNNER_DIR = Path(r"C:\tools\stage-runner")
STAGE_PYTHON = STAGE_RUNNER_DIR / ".venv" / "Scripts" / "python.exe"
STAGE_RUNNER = STAGE_RUNNER_DIR / "stage_runner.py"

# API-Watchdog: bewusst GROSSER als die interne Wall-Clock-Notbremse des Runners
# (20 min), damit eine normale Stage immer zuerst selbst endet (rc=2); feuert nur bei
# echtem Hang und beendet seither den Prozessbaum AKTIV (P4, Job-Object).
STAGE_TIMEOUT_S = 25 * 60

# Wie lange POST /abort auf die Finalisierung des Kills wartet, bevor es mit 500 antwortet.
# Normal: Kill + Reap + Pipe-Drain-Ende + JSONL-Event dauern deutlich unter einer Sekunde.
ABORT_FINALIZE_TIMEOUT_S = 30

# Pro Pipe so viel Tail in Speicher/Event halten (der Runner schreibt praktisch nur stderr).
TAIL_LIMIT = 8 * 1024

# P5: Herzschlag-Intervall des SSE-Streams (Stage laeuft sonst bis zu 20 min;
# das Event haelt den Stream sichtbar live und beweist Fortschritt an den Client).
HEARTBEAT_INTERVAL_S = 5.0

# Request-Defaults: lokal vorhandene Ollama-Modelle (verifiziert 2026-10-15: qwen3:8b
# 8.2B Q4_K_M und qwen3.8-q5 27.3B Q5_K_M; das qwen36-a3b:latest aus dem Runner-Beispiel
# existiert NICHT). qwen3:8b = schnell genug fuer interaktive Chat-Stages, tools-faehig.
DEFAULT_MODEL = "qwen3:8b"
DEFAULT_PROJECT = "coding-v2"  # Basic-Memory-Projekt, in dem das Stage-Wissen haengt

# Exit-Code -> Status. NUR hier abgeleitet; Logtext wird NICHT fuer Status geparst.
EXIT_OUTCOMES = {0: "success", 1: "error", 2: "wall_clock", 3: "max_iterations"}

# Status fuer aktiv beendete Stages (kein Runner-Urteil -> exit_code bleibt null):
STATUS_ABORTED = "aborted"            # vom Nutzer ueber POST /abort abgebrochen
STATUS_WATCHDOG = "watchdog_timeout"  # API-Watchdog hat die Stage beendet

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
    __slots__ = ("stage_id", "proc", "job_handle", "abort_event", "done_event")

    def __init__(self, stage_id: str, proc: subprocess.Popen, job_handle: int) -> None:
        self.stage_id = stage_id
        self.proc = proc
        self.job_handle = job_handle
        self.abort_event = threading.Event()  # /abort -> Worker: bitte beenden
        self.done_event = threading.Event()   # Worker -> /abort: finalisiert + persistiert


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


def _drain_capped(stream, chunks: list[bytes], limit: int) -> None:
    """Leert eine Pipe DURCHGAENGIG waehrend der Subprozess laeuft (voller Puffer =
    Deadlock) und haelt nur die letzten `limit` Bytes (Tail) fuer Event/Response."""
    try:
        total = 0
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            while total > limit and len(chunks) > 1:
                total -= len(chunks.pop(0))
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


def _terminate_live_stage(session_id: str, status: str) -> LiveStage | None:
    """Der EINZIGE Weg, eine laufende Stage zu toeten -- POST /abort UND
    Watchdog nutzen exakt diesen Mechanismus: kill-intent + abort_event setzen
    (unter _live_guard, VOR dem Kill), dann Job-Object beenden. Liefert die
    LiveStage oder None, wenn keine aktive Stage existiert (idempotent).

    Rule A: Einmal angerufen, gewinnt der Abbruch-Wunsch auch im Sub-Sekunden-
    Rennen gegen eine Natural-Completion -- der Worker persistiert dann
    exit_code=null mit dem hier gesetzten Status."""
    with _live_guard:
        live = _live_stages.get(session_id)
        if live is None:
            return None
        _kill_intent[session_id] = status
        live.abort_event.set()
        job_handle = live.job_handle
    _kill_job(job_handle)
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


def _build_task_text(session: dict, message: str) -> str:
    """Baut den Task-Text aus den letzten CONTEXT_MAX_TURNS abgeschlossenen Turns
    dieser Session (Nutzer-Nachricht + Stage-Zusammenfassung) plus der aktuellen
    Nachricht. Wird NUR in den Task-Slot geschrieben (was das Modell sieht) --
    die persistierte message_received-/session["messages"]-Historie bleibt die
    ORIGINALE, kurze Nachricht, sonst wuerde sich der Kontext bei jedem Folge-
    Turn selbst-rekursiv aufblaehen. Keine laufenden Stages (koennen in dieser
    Session gar nicht vorkommen, da Start erst nach der 409-Pruefung erfolgt)."""
    stages_by_id = {s["stage_id"]: s for s in session.get("stages", [])}
    turns: list[tuple[str, str]] = []
    for m in session.get("messages", []):
        stage = stages_by_id.get(m.get("stage_id"))
        if stage is None or stage.get("status") in (None, "running"):
            continue
        summary = stage.get("summary") or f"(Status: {stage.get('status')}, keine Zusammenfassung)"
        turns.append((m.get("content", ""), summary))
    turns = turns[-CONTEXT_MAX_TURNS:]

    if not turns:
        return message

    lines = ["Bisheriger Verlauf dieser Session (kurze Orientierung, nicht zwingend "
             "alles relevant fuer den aktuellen Auftrag):", ""]
    for user_msg, summary in turns:
        lines.append(f"Nutzer (vorher): {user_msg}")
        lines.append(f"Ergebnis: {summary}")
        lines.append("")
    history_text = "\n".join(lines)
    if len(history_text) > CONTEXT_MAX_CHARS:
        history_text = "[...gekuerzt...]\n" + history_text[-CONTEXT_MAX_CHARS:]
    return f"{history_text}--- Aktueller Auftrag ---\n{message}"


def _start_stage(session_id: str, body: MessageIn) -> tuple[StageContext, dict]:
    """P5-Startphase -- die bisherige _run_stage-Logik BIS VOR die Warte- und
    Finalisierungsphase, 1:1 unveraendert: Task-Tempdatei, Kill-Job, Subprozess,
    LiveStage-Registry, stage_record + JSONL-Events (message_received,
    stage_started), Pipe-Drainer, Watchdog. Wird vom POST /messages-Handler
    UNTER dem Session-Lock aufgerufen (404/409-Pruefung vorher). Liefert den
    Kontext fuer die Finalisierungsphase plus das stage_started-Datenobjekt
    (wird als erstes SSE-Event gepusht)."""
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

        # stdout/stderr DURCHGAENGIG leeren (voller Pipe-Puffer = Deadlock), Tails behalten.
        drainers = [
            threading.Thread(target=_drain_capped, args=(proc.stdout, out_chunks, TAIL_LIMIT), daemon=True),
            threading.Thread(target=_drain_capped, args=(proc.stderr, err_chunks, TAIL_LIMIT), daemon=True),
        ]
        for t in drainers:
            t.start()

        # P4: Watchdog-Thread -- toetet nach STAGE_TIMEOUT_S aktiv den Job.
        watchdog = threading.Thread(
            target=_watchdog_thread, args=(session_id, live, start_mono), daemon=True)
        watchdog.start()
    except Exception:
        # Startphase unvollstaendig (extrem unwahrscheinlich): Gleiche Aufraeum- und
        # Persistenzregeln wie die Fehlerklappe der Finalisierungsphase.
        log.exception("Startphase der Stage %s (Session %s) fehlgeschlagen",
                      stage_id, session_id)
        with _live_guard:
            if live.abort_event.is_set():
                fail_status = _kill_intent.pop(session_id, None) or STATUS_ABORTED
            else:
                fail_status = "incomplete"
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
        with _live_guard:
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)
        _delete_quiet(task_path)
        _close_job_handle(job_handle)  # KILL_ON_JOB_CLOSE: toetet den Prozessbaum
        live.done_event.set()
        raise

    started_data = {
        "stage_id": stage_id, "status": "running", "exit_code": None,
        "pid": proc.pid, "model": body.model, "project": body.project,
        "started_at": started_at,
    }
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
    job_handle = ctx.job_handle
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

        for t in drainers:
            t.join(timeout=5)
        stdout_tail = b"".join(out_chunks).decode("utf-8", errors="replace")
        stderr_tail = b"".join(err_chunks).decode("utf-8", errors="replace")
        _delete_quiet(task_path)

        finished_at = _utcnow()
        duration_s = round(time.monotonic() - start_mono, 1)

        with _live_guard:
            aborted = live.abort_event.is_set()
            status = _kill_intent.pop(session_id, None) if aborted else None
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
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)  # defensive (normal oben schon gepoppt)
        _close_job_handle(job_handle)  # KILL_ON_JOB_CLOSE: raeumt verbliebene Baume auf
        live.done_event.set()
        log.info("Stage %s fertig: status=%s rc=%s nach %.1f s",
                 stage_id, status, exit_code, duration_s)
        return {"session_id": session_id, "stage": stage_record}
    except Exception:
        log.exception("Finalisierung der Stage %s (Session %s) fehlgeschlagen",
                      stage_id, session_id)
        with _live_guard:
            if live.abort_event.is_set():
                fail_status = _kill_intent.pop(session_id, None) or STATUS_ABORTED
            else:
                fail_status = "incomplete"
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
        with _live_guard:
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)
        _delete_quiet(task_path)
        _close_job_handle(job_handle)  # toetet den verbliebene Prozessbaum
        live.done_event.set()
        raise


def _stage_worker(ctx: StageContext, events: queue.Queue) -> None:
    """P5-Worker-Thread (einer pro POST /messages): finalisiert die in der
    Startphase gestartete Stage und liefert das Ergebnis als Queue-Items an den
    SSE-Generator -- genau EIN Terminal-Event, danach der Sentinel. Laeuft OHNE
    Session-Lock; eine Client-Trennung aendert nichts am Runner-Lauf (die Stage
    laeuft weiter, Finalisierung/Persistenz unveraendert -- Abbruch bleibt
    POST /abort)."""
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
    events.put(("stage_completed", result["stage"]))
    events.put(None)  # Sentinel: Stream-Ende


async def _sse_stream(events: queue.Queue, started_mono: float) -> AsyncIterator[str]:
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
    events: queue.Queue = queue.Queue()
    with lock:
        if _sessions.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if _running_stage(_sessions[session_id]) is not None:
            raise HTTPException(status_code=409, detail="stage already running for this session")
        ctx, started_data = _start_stage(session_id, body)
    # stage_started vorab pushten (vor der Worker-Finalisierung; garantiert
    # Reihenfolge und dass es auch bei sofortigem Abbruch ankommt).
    events.put(("stage_started", started_data))
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
