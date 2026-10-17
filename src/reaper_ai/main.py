# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

r"""Reaper API -- P5: HTTP skeleton + sessions + JSONL persistence + stage as a subprocess
+ error/abort handling (process-tree kill, active watchdog, POST /abort)
+ SSE status output for POST /sessions/{id}/messages.

Target architecture v1 (coding-v2, stage-runner, decision_record):
- POST /sessions                  create a new chat session (session_created event)
- GET  /sessions/{id}             read a session, 404 if unknown
- POST /sessions/{id}/messages    run a stage for the user's task and deliver the
                                  progress as an SSE stream (P5):
                                  text/event-stream, a stage_started event
                                  immediately, a heartbeat event every 5 s, and
                                  at the end exactly ONE terminal event
                                  stage_completed with the full stage object
                                  (status/exit_code/finished_at/duration_s/
                                  summary/stdout_tail/stderr_tail). 404/409 stay
                                  BEFORE the stream starts. No token streaming,
                                  no tool events (deliberate, target arch v1).
- POST /sessions/{id}/abort       abort the running stage in a controlled way (P4);
                                  the process tree incl. pwsh child processes is killed

A stage = the existing C:\tools\stage-runner\stage_runner.py as a separate
subprocess (with its own venv there). The runner code stays unchanged and is NOT
imported. Exit codes 0-3 are the ONLY status source (target architecture); aborted
stages (user abort or API watchdog) have exit_code=null because there is no runner
verdict. The stdout/stderr pipes are drained continuously (full pipe buffer =
deadlock) and provide diagnostic tails plus the finish_stage summary as display
text.

Process-tree kill (Windows): every stage runs in a Windows job object with
KILL_ON_JOB_CLOSE. TerminateJobObject therefore kills the whole tree atomically
(main process + all pwsh child processes, including ones started later). If the API
process itself dies, the job handles close and the OS kills the trees -- no more
orphan processes after an API crash.

Status vocabulary: success/error/wall_clock/max_iterations (exit code 0-3),
aborted (user via /abort), watchdog_timeout (API watchdog), incomplete (only:
stage was running when the API restarted), unknown_rc_N (defensive, should not
normally occur).

Persistence: one JSONL file per session under <data-dir>/sessions/<session_id>.jsonl,
append-only, one line per event. Status changes are appended, never overwritten.
On server start all files are read and the state is reconstructed from the full
event sequence; stages that were still "running" at that point are marked
"incomplete" and are NOT re-run automatically.

P5 flow: POST /messages runs the start phase synchronously UNDER the session lock
(the 404/409 check stays BEFORE the stream starts; subprocess + kill job +
drainers + watchdog + JSONL events message_received/stage_started) and then
returns a StreamingResponse. Waiting for the exit, finalization and persisting
the terminal event run in one worker thread per request and deliver the result
as queue items to the async SSE generator (stage_started is pushed up front in
the request thread). A client disconnect during the stage does not affect the
runner -- aborting is still POST /abort.

Added since P5: limited chat context for follow-up tasks (_build_task_text,
preview via GET /sessions/{id}/context), live log_line events with a bounded
queue (BoundedSseQueue, log_dropped on overload), GET /health for the GUI status
light and /vendor for the locally bundled GUI libraries. Endpoint docs: docs/api.md.
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
from starlette.staticfiles import StaticFiles

from . import __version__
from .config import DEFAULT_RUNNER_DIR, data_dir, default_runner_python, ollama_host_port

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reaper-api")

# Packaging: code and GUI live in the installed package (APP_DIR), runtime data
# (sessions, temp task files) in the data directory DATA_DIR OUTSIDE the package.
# The CLI (reaper_ai/cli.py) resolves configuration/data directory and passes them
# in as env vars; main.py reads ONLY env vars (see reaper_ai/config.py).
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = data_dir()
SESSIONS_DIR = DATA_DIR / "sessions"
TASK_TMP_DIR = DATA_DIR / "tasks"  # temp task files for --task @file (Windows argv limit)

# Stage runner: existing code, own venv, own directory -- only as a subprocess.
# Directory configurable via REAPER_STAGE_RUNNER_DIR (or runner_dir in config.toml);
# Python/script can additionally be overridden individually (regression tests in
# test-queue/ use this for a fake runner). Without configuration it is still
# C:\tools\stage-runner as before.
STAGE_RUNNER_DIR = Path(os.environ.get("REAPER_STAGE_RUNNER_DIR") or DEFAULT_RUNNER_DIR)
STAGE_PYTHON = Path(os.environ["REAPER_STAGE_PYTHON"]) if os.environ.get("REAPER_STAGE_PYTHON") \
    else default_runner_python(STAGE_RUNNER_DIR)
STAGE_RUNNER = Path(os.environ["REAPER_STAGE_RUNNER_SCRIPT"]) if os.environ.get("REAPER_STAGE_RUNNER_SCRIPT") \
    else STAGE_RUNNER_DIR / "stage_runner.py"

# API watchdog: deliberately LARGER than the runner's internal wall-clock emergency
# brake (20 min), so a normal stage always ends on its own first (rc=2); it only fires
# on a real hang and since P4 kills the process tree ACTIVELY (job object).
# Can be shortened via env var ONLY for regression tests (test-queue/test_live_api.py,
# watchdog-under-full-queue case) -- without the variable it stays 25 min.
STAGE_TIMEOUT_S = int(os.environ.get("REAPER_STAGE_TIMEOUT_S", str(25 * 60)))

# How long POST /abort waits for the kill to be finalized before answering with 500.
# Normally kill + reap + end of pipe drain + JSONL event take well under a second.
ABORT_FINALIZE_TIMEOUT_S = 30

# Tail kept in memory/event per pipe (the runner writes practically only to stderr).
TAIL_LIMIT = 8 * 1024

# Line buffer of the stderr drainer (live events + summary capture): a single line
# without '\n' must not grow memory without bound. From this length on the line is
# capped (the rest up to the next '\n' is discarded) and marked with
# _LINE_TRUNCATED_SUFFIX. Same size as the summary-capture cap.
_LINE_BUFFER_LIMIT = 64 * 1024
_LINE_TRUNCATED_SUFFIX = " ...[line truncated]"

# P5: heartbeat interval of the SSE stream (otherwise a stage runs for up to 20 min
# silently; the event keeps the stream visibly live and proves progress to the client).
HEARTBEAT_INTERVAL_S = 5.0

# Bounded SSE event queue (P1 fix): with a slow/disconnected client AND a high log
# volume at the same time (e.g. a stage with many tool calls) the previously
# unbounded queue.Queue() grew without limit -- log_line events are now dropped
# on overload (counted, reported at stage end as one log_dropped event), while
# stage_started/stage_completed/sentinel are GUARANTEED to be delivered (evicting
# older log_line entries if necessary). 1000 entries ~ several minutes of normal
# log rate at 5-10 lines/s -- generous enough for short delays, small enough to
# hard-limit memory per stage.
SSE_QUEUE_MAXSIZE = 1000

# Request defaults: locally available Ollama models (verified 2026-10-15: qwen3:8b
# 8.2B Q4_K_M and qwen3.8-q5 27.3B Q5_K_M; the qwen36-a3b:latest from the runner example
# does NOT exist). Since the move away from Jan (2026-10-16) Ollama is the ONLY
# model source -- qwen3.8-q5 is Reaper's main model (role=main, full persona),
# qwen3:8b remains the fast default model for run_substage worker stages.
DEFAULT_MODEL = "qwen3.8-q5:latest"
DEFAULT_PROJECT = "coding-v2"  # Basic Memory project that holds the stage knowledge

# Exit code -> status. Derived ONLY here; log text is NOT parsed for status.
EXIT_OUTCOMES = {0: "success", 1: "error", 2: "wall_clock", 3: "max_iterations"}

# Status for actively terminated stages (no runner verdict -> exit_code stays null):
STATUS_ABORTED = "aborted"            # aborted by the user via POST /abort
STATUS_WATCHDOG = "watchdog_timeout"  # the API watchdog terminated the stage

# Race fix (P1): sentinel value for _kill_intent[session_id] -- marks the
# LINEARIZATION POINT between natural process end and abort (see
# _finalize_stage/_terminate_live_stage). _kill_intent therefore no longer only
# says "which abort reason" but is the ONLY binding decision:
# None = still open, _NATURAL = natural end reached the point first
# (an abort arriving afterwards is too late), any other string = an abort
# reason reached the point first (first abort reason wins).
_NATURAL = "__natural_completion__"

CREATE_NO_WINDOW = 0x08000000  # Windows: no console window (like the runner for pwsh)

# Protocol marker written by stage_runner.py (finish_stage). It is part of the
# runner's output format, NOT a Reaper-AI message -- it must stay exactly as the
# runner emits it (German), otherwise the summary is no longer found.
_FINISH_MARKER = "=== STAGE BEENDET"
_TS_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ", flags=re.M)

# P1 fix (result loss with long logs): generous cap ONLY for the finish_stage
# summary captured live (see _SummaryCapture), independent of the general
# diagnostic tail (TAIL_LIMIT = 8 KiB), which can lose the summary with an
# extensive log because it only keeps the LAST few KB. 64 KiB leaves even longer
# normal summaries untruncated; only the case (not seen in practice) where stderr
# keeps being written to without limit after finish_stage gets capped.
_SUMMARY_CAPTURE_LIMIT = 64 * 1024


class _SummaryCapture:
    """Captures the finish_stage summary (marker '=== STAGE BEENDET') LIVE while
    draining the stderr pipe -- independent of the bounded diagnostic tail, which
    only holds the last few KB of the WHOLE log and can push the summary out with
    extensive output. Works line by line on already fully decoded lines (see
    _drain_capped) -- marker and UTF-8 multi-byte characters can therefore never
    be cut at a chunk boundary (splitting there happens exclusively on byte 0x0A,
    which in UTF-8 is never part of a multi-byte character). Does NOT keep the
    full log in memory: only the lines from the marker on, limited to
    _SUMMARY_CAPTURE_LIMIT characters."""

    __slots__ = ("_found", "_parts", "_size", "_truncated")

    def __init__(self) -> None:
        self._found = False
        self._parts: list[str] = []
        self._size = 0
        self._truncated = False

    def feed_line(self, line: str) -> None:
        if not self._found:
            if _FINISH_MARKER in line:
                self._found = True  # discard the rest of the marker line ("(success=..) ===").
            return
        if self._truncated:
            return
        remaining = _SUMMARY_CAPTURE_LIMIT - self._size
        if remaining <= 0:
            self._truncated = True
            self._parts.append("...[truncated]...")
            return
        if len(line) > remaining:
            # A single line itself crosses the cap -- keep only the fitting rest
            # instead of first appending the whole (potentially huge) line and
            # only then noticing that the cap was exceeded (otherwise ONE
            # pathological line could blow the memory cap in a single step).
            self._parts.append(line[:remaining])
            self._size += remaining
            self._truncated = True
            self._parts.append("...[truncated]...")
            return
        self._parts.append(line)
        self._size += len(line) + 1

    @property
    def text(self) -> str | None:
        """Summary without timestamp prefix, or None if the marker has not been
        seen (yet)."""
        if not self._found:
            return None
        block = "\n".join(self._parts)
        text = _TS_PREFIX.sub("", block).strip()
        return text or None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_event(etype: str, data: dict | None = None) -> dict:
    """One JSONL event: exactly one JSON object per line."""
    return {"ts": _utcnow(), "type": etype, "data": data or {}}


def _append_event(session_id: str, event: dict) -> None:
    """Appends ONE event to the session file (the file is never overwritten)."""
    path = SESSIONS_DIR / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _apply_event(session: dict, event: dict) -> None:
    """Folds ONE JSONL event into the session state.

    Reconstruction = running all lines of the file through this function in
    order; later events extend/overwrite earlier ones. Unknown types are
    deliberately ignored (reserved for later phases).
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
            # Defensive: terminal event without stage_started (should not normally occur).
            log.warning("stage_completed without stage_started for %s -- folded on its own",
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
    """Reads all session files on server start and reconstructs the state from
    the full event sequence (not just the last line)."""
    sessions: dict[str, dict] = {}
    for path in sorted(SESSIONS_DIR.glob("*.jsonl")):
        session: dict = {
            "session_id": path.stem,
            "created_at": None,
            "messages": [],  # role/content/stage_id/ts from message_received
            "stages": [],    # stage_id/status/... from stage_started + stage_completed
        }
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s line %d: not valid JSON, line skipped", path.name, lineno)
                continue
            _apply_event(session, event)
        if session["created_at"] is None:
            log.warning("%s: no session_created event, file not loaded as a session", path.name)
            continue
        for stage in session["stages"]:
            if stage.get("status") == "running":
                # Target architecture: stages whose completion is unclear (e.g. after an
                # API crash) are marked incomplete and NOT re-run automatically.
                # (Since P4 the job object kills the trees along with the API; the status
                # still documents that there is no clean runner verdict.)
                stage["status"] = "incomplete"
                log.warning("%s: stage %s was still 'running' at restart -> 'incomplete'",
                            path.name, stage.get("stage_id"))
        sessions[session["session_id"]] = session
    return sessions


# ---------------------------------------------------------------------------
# P4: process-tree kill via Windows job object (KILL_ON_JOB_CLOSE)
#
# Decision job object instead of psutil process-tree kill:
# - atomic at kernel level: TerminateJobObject kills ALL processes of the job
#   (main process + all pwsh children/grandchildren, including ones started
#   BETWEEN enumeration and kill). The psutil route (enumerate children
#   recursively + terminate individually) has a race window: stage_runner.py can
#   start pwsh at any time (tool timeout up to 600 s), late children survive there.
# - no new dependency: ctypes is stdlib; psutil would be a new venv dependency
#   against the KISS principle of the target architecture.
# - side effect: if the API process itself dies (crash/Ctrl+C), the job handles
#   close and the OS terminates the trees -- no more orphan processes after an
#   API crash (before P4 only the 'incomplete' marking on restart was left; the
#   orphaned process simply kept running).
# (Deliberate assumption: a privileged process could theoretically "break out"
#  of the job; not realistic for a local single-user system.)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # SetInformationJobObject class (WinAPI enum JobObjectExtendedLimitInformation)
    _PROCESS_SET_QUOTA = 0x0100                # required for AssignProcessToJobObject
    PROCESS_TERMINATE = 0x0001                 # fallback termination of the main process

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

    # P1 fix "atomic job assignment": on Windows subprocess.Popen() closes the
    # primary thread handle from CreateProcess IMMEDIATELY internally
    # (_execute_child) and does not expose it publicly anywhere -- so there is NO
    # way to resume a process started suspended via Popen(creationflags=
    # CREATE_SUSPENDED) through the Popen API itself (checked before the
    # implementation, see test-queue/experiment_suspend_resume.py).
    # Workaround without a home-grown CreateProcessW: find the thread AFTERWARDS
    # via its thread ID (Toolhelp32Snapshot) and reopen it via OpenThread() --
    # only needs the (public, already used) process handle via proc.pid, no
    # private CPython internals. CREATE_SUSPENDED itself is not available in this
    # Python build as subprocess.CREATE_SUSPENDED nor as _winapi.CREATE_SUSPENDED
    # (unlike e.g. CREATE_NO_WINDOW) -- the raw Win32 value 0x00000004 is however
    # a stable, publicly documented WinBase.h constant, defined locally just like
    # CREATE_NO_WINDOW above.
    CREATE_SUSPENDED = 0x00000004
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD


def _create_kill_job() -> int:
    """Creates a job object with KILL_ON_JOB_CLOSE (closing the handle kills the tree)."""
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(f"CreateJobObjectW failed (err={ctypes.get_last_error()})")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(handle, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(f"SetInformationJobObject failed (err={err})")
    return handle


def _close_job_handle(handle: int) -> None:
    kernel32.CloseHandle(handle)


def _assign_process_to_job(job_handle: int, proc: subprocess.Popen) -> None:
    """Assigns the stage process to the kill job. Everything this process starts
    LATER (pwsh children) automatically belongs to the same job (child processes
    inherit job membership) -- so one handle covers the whole tree."""
    if kernel32.AssignProcessToJobObject(job_handle, proc._handle):
        return
    err = ctypes.get_last_error()
    # Fallback: open a fresh handle with the rights required for the assignment
    # (PROCESS_SET_QUOTA); in practice Popen's own handle is sufficient.
    h = kernel32.OpenProcess(_PROCESS_SET_QUOTA, False, proc.pid)
    if h and kernel32.AssignProcessToJobObject(job_handle, h):
        kernel32.CloseHandle(h)
        return
    if h:
        kernel32.CloseHandle(h)
    raise OSError(f"AssignProcessToJobObject failed (err={err})")


def _resume_all_threads(pid: int) -> int:
    """Counterpart to Popen(creationflags=CREATE_SUSPENDED): finds all threads of
    the process via a Toolhelp32 snapshot (thread-ID based, since Popen no longer
    has a thread handle -- see the comment at CREATE_SUSPENDED above) and resumes
    them via a freshly opened handle. A freshly created suspended process has
    exactly ONE thread (the main thread); we still resume all threads belonging
    to the process, not just the first one found, to make no assumption about the
    thread count. Returns the number of successfully resumed threads (0 = nothing
    found/resumed, to be treated as an error by the caller -- otherwise the
    process would stay frozen forever)."""
    snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise OSError(f"CreateToolhelp32Snapshot failed (err={ctypes.get_last_error()})")
    resumed = 0
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        found = kernel32.Thread32First(snap, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                th = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if th:
                    if kernel32.ResumeThread(th) != 0xFFFFFFFF:
                        resumed += 1
                    else:
                        log.warning("ResumeThread failed for thread %d (err=%d)",
                                    entry.th32ThreadID, ctypes.get_last_error())
                    kernel32.CloseHandle(th)
            found = kernel32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return resumed


class LiveStage:
    """Registry entry of a running stage (key: session_id).

    The worker thread (post_message) owns proc/job_handle and is the ONLY one
    that kills and persists (under the session lock, exactly once). /abort only
    sets abort_event and waits for done_event -- this keeps the JSONL order
    stage_started/stage_completed unchanged.
    """
    __slots__ = ("stage_id", "proc", "job_handle", "abort_event", "done_event", "job_closed")

    def __init__(self, stage_id: str, proc: subprocess.Popen, job_handle: int) -> None:
        self.stage_id = stage_id
        self.proc = proc
        self.job_handle = job_handle
        self.abort_event = threading.Event()  # /abort -> worker: please terminate
        self.done_event = threading.Event()   # worker -> /abort: finalized + persisted
        # Race fix (P1, point A): protects against TerminateJobObject() on an
        # already closed handle. Read/set ONLY under _live_guard (see
        # _kill_job_locked/_close_job_handle_locked) -- this makes termination
        # and handle closing mutually exclusive, never concurrent.
        self.job_closed = False


# Runtime state: in-memory index of the persisted sessions; filled at start by
# _load_sessions() and extended by the endpoints.
_sessions: dict[str, dict] = {}

# Per-session lock: serializes the stages of ONE session (single uvicorn process,
# FastAPI runs synchronous endpoints in the thread pool).
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()

# P4: registry of running stages for POST /abort (session_id -> LiveStage, at most
# one per session). Deliberately separate from the session lock, because /abort must
# NOT block behind the lock of a running stage (the worker holds it until the end).
_live_stages: dict[str, LiveStage] = {}
_live_guard = threading.Lock()
# P4: kill-intent registry (session_id -> status word "aborted"/"watchdog_timeout").
# Set by _terminate_live_stage under _live_guard (BEFORE the kill, rule A)
# and popped by the worker during finalization -- same locking scheme as
# _live_stages. The worker always reads/deletes the entry under _live_guard.
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
        log.warning("stage_runner.py incomplete (%s / %s) -- POST /messages will return 500",
                    STAGE_PYTHON, STAGE_RUNNER)
    _sessions.update(_load_sessions())
    if _sessions:
        log.info("%d session(s) reconstructed from %s", len(_sessions), SESSIONS_DIR)
    yield


# Loopback only: local single-user system, no authentication (architecture constraint).
app = FastAPI(
    title="Reaper API",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

WEB_DIR = APP_DIR / "web"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serves the single-page chat GUI (web/index.html); it talks ONLY to the
    public endpoints of this API (sessions/messages/abort) and knows nothing about
    stage_runner.py/Ollama/Basic Memory."""
    return FileResponse(WEB_DIR / "index.html")


# Locally bundled GUI libraries (marked, DOMPurify, highlight.js) -- purely
# static, no CDN dependency at runtime.
app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")


# From REAPER_OLLAMA_URL (or ollama_url in config.toml), default 127.0.0.1:11434.
# Only for the diagnostic status light: stage_runner.py uses its own hard-coded URL.
OLLAMA_HEALTH_HOST, OLLAMA_HEALTH_PORT = ollama_host_port()


def _tcp_reachable(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


@app.get("/health", include_in_schema=False)
def health() -> dict:
    """Purely diagnostic status for the GUI status light -- NO functional
    dependency: main.py never calls Ollama itself (that is done exclusively by
    the stage_runner.py subprocess); here only a TCP connect checks whether the
    port is open, so the GUI can show why a stage would hang BEFORE the user
    sends a message."""
    return {
        "api": "ok",
        "ollama": "ok" if _tcp_reachable(OLLAMA_HEALTH_HOST, OLLAMA_HEALTH_PORT) else "down",
        "stage_runner": "ok" if STAGE_PYTHON.exists() and STAGE_RUNNER.exists() else "missing",
    }


class MessageIn(BaseModel):
    """Body of POST /sessions/{id}/messages. model/project/think can be overridden;
    defaults = locally available Ollama model, standard memory project, reasoning OFF
    (deterministic; True = the model thinks along -- practically unbounded with Qwen3/Q5)."""

    message: str = Field(min_length=1, description="User task text (= stage task)")
    model: str = Field(default=DEFAULT_MODEL, min_length=1,
                       description=f"Ollama model name (default {DEFAULT_MODEL})")
    project: str = Field(default=DEFAULT_PROJECT, min_length=1,
                         description=f"Basic Memory project (default {DEFAULT_PROJECT})")
    # think is ALWAYS passed to stage_runner.py explicitly (--think on/off):
    # if the key is missing, the runner runs with "auto" and the model uses its own
    # reasoning default (Qwen3/Q5: practically unbounded) -- E2E bug, fixed 2026-10-16.
    think: bool = Field(default=False,
                        description="Model reasoning/thinking (Ollama 'think'); off by default")

    @field_validator("message")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty or whitespace only")
        return v


class BoundedSseQueue:
    """Bounded SSE event queue with two delivery classes:

    - log_line (via put_log_line): best effort, NEVER blocking. When the queue
      is full the line is dropped and only counted (no queue.put() with unbounded
      waiting, no individual warning per line -- requirement 3/4). The stderr
      drainer (_drain_capped) therefore always stays runnable, even if the SSE
      client does not consume; the stderr pipe keeps being drained independently
      of the consumer (no pipe-deadlock risk).
    - priority (via put_priority): stage_started/stage_completed/sentinel
      MUST arrive -- when the queue is full a log_line entry is evicted
      SPECIFICALLY, NEVER another priority event. Never blocks indefinitely,
      since the queue is always finite and evicts instead of waiting.

    CORRECTION (found by a targeted test, see test_bounded_queue.py
    test_priority_eviction_never_removes_priority_event): the original version
    blindly evicted the OLDEST entry, regardless of its type. Since stage_started
    is ALWAYS enqueued as the first element (_start_stage, before the drainer
    threads), it is exactly the oldest entry in a queue that later fills up -- a
    subsequent put_priority(stage_completed) would therefore have EVICTED
    stage_started instead of a log_line entry. _evict_one_log_line_locked()
    therefore specifically looks for the first log_line entry (re-enqueuing
    skipped non-log_line entries in their original order) instead of blindly
    removing the head element.

    All mutating operations (put_log_line, put_priority/eviction, dropped
    counter) run under ONE shared lock, so the eviction (multi-step: search the
    queue, re-enqueue non-victims) is atomic with respect to other producers --
    otherwise a concurrent put_log_line() call could insert another entry in the
    middle of the search and corrupt the result. get() (consumer side) stays
    lock-free, queue.Queue is already thread-safe internally for that; with
    maxsize=1000 the eviction is a short, bounded scan (microseconds), not an
    unbounded wait."""

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
                        # Practically unreachable: there are at most 4 priority
                        # events per stage (stage_started, log_dropped,
                        # stage_completed, sentinel) with maxsize=1000 -- so there
                        # is always at least one log_line entry to evict. Last
                        # resort (e.g. with a deliberately tiny maxsize in a test),
                        # so that put_priority() NEVER blocks: evict the oldest entry.
                        try:
                            self._q.get_nowait()
                        except queue.Empty:
                            pass

    def _evict_one_log_line_locked(self) -> bool:
        """Caller already holds self._lock. Searches from the head of the queue
        for the FIRST log_line entry and removes ONLY that one; all non-log_line
        entries skipped along the way (stage_started etc.) are re-enqueued in
        their original order. True if a log_line entry was removed (room made),
        otherwise False."""
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
                continue  # do NOT re-enqueue -- this frees the slot
            held.append(old)
        for h in held:
            self._q.put_nowait(h)  # always fits again -- we removed at most 1
        return found

    def pop_dropped(self) -> int:
        """Returns the number of log_line events dropped since the last call and
        resets the counter -- for the optional log_dropped event."""
        with self._lock:
            n = self._dropped
            self._dropped = 0
            return n

    def get(self, timeout: float):
        """Like queue.Queue.get(timeout=...): blocks until an item arrives or
        raises queue.Empty on timeout -- unchanged consumer behavior."""
        return self._q.get(timeout=timeout)


def _drain_capped(stream, chunks: list[bytes], limit: int,
                   events: "BoundedSseQueue | None" = None,
                   summary_capture: "_SummaryCapture | None" = None) -> None:
    """Drains a pipe CONTINUOUSLY while the subprocess runs (full buffer =
    deadlock) and keeps only the last `limit` bytes (tail) for event/response.
    With `events`: additionally pushes EVERY complete line IMMEDIATELY as its own
    SSE event ('log_line') -- live visibility in the GUI, e.g. whether/when
    run_substage runs, without waiting for the stage to end. Deliberately used
    only for stderr (stage_runner.py._log() writes there, stdout is empty).
    With `summary_capture`: additionally feeds the same, already fully decoded
    lines live into the finish_stage capture (P1 fix: result loss with long
    logs) -- independent of the byte cap `limit` above, which only bounds the
    general diagnostic tail (the LAST `limit` bytes) and can push the summary
    out with an extensive log.
    A last line without a trailing '\n' is also processed when the pipe ends;
    individual lines are limited to _LINE_BUFFER_LIMIT bytes."""
    buf = bytearray()
    line_truncated = False  # current line has already reached _LINE_BUFFER_LIMIT

    def _emit_line() -> None:
        # Hand a (possibly capped) line to live events and the summary capture.
        text = bytes(buf).decode("utf-8", errors="replace").rstrip("\r")
        if line_truncated:
            text += _LINE_TRUNCATED_SUFFIX
        if summary_capture is not None:
            summary_capture.feed_line(text)
        if text and events is not None:
            events.put_log_line({"text": text})

    def _append_capped(segment: bytes) -> None:
        # Appends at most up to _LINE_BUFFER_LIMIT bytes to the current line;
        # the rest of a pathologically long line is discarded up to the next '\n'
        # (the byte tail above keeps it independently).
        nonlocal line_truncated
        if line_truncated:
            return
        room = _LINE_BUFFER_LIMIT - len(buf)
        if len(segment) > room:
            buf.extend(segment[:room])
            line_truncated = True
        else:
            buf.extend(segment)

    try:
        total = 0
        while True:
            # read1() instead of read(): read() on a BufferedReader tries to collect
            # the FULL 65536 bytes (so it blocks until either that much output was
            # produced or the pipe closed) -- useless for live streaming, a stage with
            # little output would NEVER have shown a line live until it was done.
            # read1() returns immediately with whatever is available (at least 1 byte).
            chunk = stream.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            while total > limit and len(chunks) > 1:
                total -= len(chunks.pop(0))
            if events is not None or summary_capture is not None:
                # Split exclusively on byte 0x0A: a UTF-8 multi-byte sequence never
                # contains this byte as a continuation or start byte, so decode()
                # here always runs on a complete line that was never cut at a chunk
                # boundary -- marker detection and live events are therefore
                # insensitive to arbitrary read1() chunk boundaries.
                # (Exception: a line capped because of _LINE_BUFFER_LIMIT can
                # end in the middle of a character -> errors="replace".)
                start = 0
                while True:
                    nl = chunk.find(b"\n", start)
                    if nl == -1:
                        _append_capped(chunk[start:])
                        break
                    _append_capped(chunk[start:nl])
                    _emit_line()
                    buf.clear()
                    line_truncated = False
                    start = nl + 1
    except Exception as exc:
        log.warning("Pipe drain interrupted: %s", exc)
    finally:
        # Do not discard a last line without a trailing '\n' (e.g. the runner ends
        # with print(..., end="") or is terminated mid-line) -- otherwise the last
        # line of the summary might be missing.
        if (buf or line_truncated) and (events is not None or summary_capture is not None):
            try:
                _emit_line()
            except Exception as exc:
                log.warning("Last pipe line could not be processed: %s", exc)
        try:
            stream.close()
        except OSError:
            pass


def _delete_quiet(path: str | None) -> None:
    """Delete the temp task file -- on success AND on failure (P3 requirement)."""
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Temp task file %s could not be deleted: %s", path, exc)


def _extract_summary(rc: int, stderr_tail: str, captured: str | None = None) -> str:
    """finish_stage summary as display text.

    The STATUS NEVER comes from log text (always the exit code); here only the text
    after the '=== STAGE BEENDET' marker is taken over for display (target arch v1).

    `captured` (P1 fix: result loss with long logs) is the summary captured LIVE
    while draining (see _SummaryCapture), independent of the bounded
    `stderr_tail` diagnostic tail -- if present, it takes precedence.
    The tail search below remains as a fallback (e.g. if no capture object was
    passed) as well as for the rc-based fallback texts below.
    """
    if captured:
        return captured
    idx = stderr_tail.find(_FINISH_MARKER)
    if idx != -1:
        block = stderr_tail[idx + len(_FINISH_MARKER):]
        nl = block.find("\n")  # discard the rest of the marker line "(success=...)"
        if nl != -1:
            block = block[nl + 1:]
        text = _TS_PREFIX.sub("", block).strip()
        if text:
            return text
    if rc == 0:
        return "(finish_stage summary not found in the stderr tail)"
    fallback = {
        1: "Runner error or success=false (details in stderr_tail).",
        2: "Aborted: wall-clock limit (20 min) reached, no finish_stage call.",
        3: "Aborted: maximum number of iterations reached, no finish_stage call.",
    }
    return fallback.get(rc, f"Unknown exit code {rc} (details in stderr_tail).")

# ---------------------------------------------------------------------------
# Endpoints + stage worker (P3 logic; P4: kill job per stage, abort_event,
# active watchdog, POST /abort)
# ---------------------------------------------------------------------------


def _kill_job(job_handle: int) -> None:
    """TerminateJobObject on the held job handle: kills ALL processes of the job
    atomically (main process + all child processes incl. pwsh stages).
    Only the /abort handler and the watchdog call this -- never the worker."""
    if not kernel32.TerminateJobObject(job_handle, 1):
        log.warning("TerminateJobObject failed (err=%d) -- tree may still be running",
                    ctypes.get_last_error())


def _kill_job_locked(live: "LiveStage") -> None:
    """Race fix (P1, point A): caller MUST already hold _live_guard. Calls
    TerminateJobObject only if the handle has not been closed yet -- prevents a
    kill from hitting a handle already closed (by _finalize_stage)
    (use-after-close/handle reuse). Since both this function and
    _close_job_handle_locked() run exclusively under _live_guard, terminating
    and closing the handle are mutually exclusive."""
    if live.job_closed:
        return
    _kill_job(live.job_handle)


def _close_job_handle_locked(live: "LiveStage") -> None:
    """Race fix (P1, point A): caller MUST already hold _live_guard. Closes the
    handle at most once (job_closed flag) -- after that _kill_job_locked can
    never touch this handle again, no matter which thread gets _live_guard
    next."""
    if live.job_closed:
        return
    _close_job_handle(live.job_handle)
    live.job_closed = True


def _terminate_live_stage(session_id: str, status: str) -> LiveStage | None:
    """The ONLY way to kill a running stage -- POST /abort AND the watchdog use
    exactly this mechanism. Returns the LiveStage, or None if there is no active
    stage OR the natural end has already reached the linearization point first
    (point C, too late).

    Race fix (P1): setting the kill intent, setting abort_event AND the actual
    kill (_kill_job_locked) now all run under THE SAME _live_guard acquisition
    -- previously the kill was outside the lock and could race a concurrent
    _close_job_handle() in _finalize_stage for the handle (point A,
    use-after-close/handle reuse). _kill_intent is now the ONLY binding
    decision (see _NATURAL/_finalize_stage):
    - already _NATURAL: the natural end reached the linearization point first --
      the abort is too late, the result stays unchanged (point C). No kill
      needed, the process is already dead (confirmed by _wait_proc).
    - already another status: an earlier abort reason won (point B, first one
      wins) -- we do NOT overwrite it, but still set abort_event/kill again
      (idempotent, harmless in case the first attempt has not reached the
      process yet).
    - None (open): we are first -- our `status` wins bindingly."""
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
    """Per-stage watchdog (P4): wakes up exactly after STAGE_TIMEOUT_S -- or
    earlier if the worker has finished finalizing the stage (done_event is set
    at the end) -- and then kills the process tree ACTIVELY instead of letting
    the stage run (before P4: only status='incomplete' was set). Normally the
    runner fires first on its own (20 min emergency brake, rc=2); only a real
    hang ends here."""
    remaining = STAGE_TIMEOUT_S - (time.monotonic() - start_mono)
    if remaining > 0:
        live.done_event.wait(timeout=remaining)
    if live.done_event.is_set():
        return  # stage finished normally -- nothing to do
    log.warning("Watchdog: stage %s (session %s) exceeds %.0f s -- killing job",
                live.stage_id, session_id, STAGE_TIMEOUT_S)
    term = _terminate_live_stage(session_id, STATUS_WATCHDOG)
    if term is not None:
        term.done_event.wait(timeout=ABORT_FINALIZE_TIMEOUT_S)


def _wait_proc(proc: subprocess.Popen, session_id: str, deadline: float) -> int | None:
    """proc.poll() in a 1 s loop instead of a blocking wait (P4 spec): the worker
    observes the end without blocking indefinitely. IMPORTANT: poll(), NOT
    wait(timeout) -- wait(timeout) RAISES TimeoutExpired while the process is
    running (it never returns None) and would have crashed every stage
    immediately. Returns the exit code; if the deadline is exceeded, the worker
    kills ONCE by itself via _terminate_live_stage (same mechanism as /abort) --
    only a backstop in case the watchdog is not there for some reason."""
    backstop_done = False
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        if time.monotonic() >= deadline and not backstop_done:
            log.error("Stage (session %s) past deadline, watchdog not there yet -- "
                      "backstop kill by the worker", session_id)
            _terminate_live_stage(session_id, STATUS_WATCHDOG)
            backstop_done = True
        time.sleep(1.0)


@app.post("/sessions", status_code=201)
def create_session() -> dict:
    """Create a new chat session (P3, logic unchanged). The session_created event
    is the persistence basis of the reconstruction."""
    session_id = str(uuid.uuid4())
    session = {"session_id": session_id, "created_at": _utcnow(),
               "messages": [], "stages": []}
    _append_event(session_id, _new_event("session_created"))
    _sessions[session_id] = session
    return session


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    """Read a session (P3, logic unchanged); 404 for an unknown id."""
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return session


@app.get("/sessions/{session_id}/context", include_in_schema=False)
def get_context_preview(session_id: str) -> dict:
    """Shows what the limited context (_build_task_text) would prepend to a next
    message RIGHT NOW -- a pure preview, starts no stage. For the context panel
    of the GUI."""
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
    """Intermediate state of a stage between the start phase (request thread,
    under the session lock) and the finalization phase (worker thread, no lock)."""

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
    summary_capture: "_SummaryCapture" = field(default_factory=_SummaryCapture)


def _sse_frame(etype: str, data: dict) -> str:
    """Standard SSE frame: 'event: <type>\ndata: <one JSON line>\n\n'."""
    return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# Follow-up package "limited context for follow-up tasks" (target architecture v1,
# deliberately NOT part of P5): deliberately NO full history -- only a short
# orientation about the last few completed turns, so that a follow-up task is not
# completely context-free. Durable knowledge remains the job of the model itself
# (memory_write), which this does NOT replace.
CONTEXT_MAX_TURNS = 5
CONTEXT_MAX_CHARS = 6000


def _collect_context_turns(session: dict) -> list[tuple[str, str]]:
    """The last CONTEXT_MAX_TURNS COMPLETED turns of this session (user message,
    stage summary). No running stages (they cannot occur here at all, since a
    start only happens after the 409 check)."""
    stages_by_id = {s["stage_id"]: s for s in session.get("stages", [])}
    turns: list[tuple[str, str]] = []
    for m in session.get("messages", []):
        stage = stages_by_id.get(m.get("stage_id"))
        if stage is None or stage.get("status") in (None, "running"):
            continue
        summary = stage.get("summary") or f"(Status: {stage.get('status')}, no summary)"
        turns.append((m.get("content", ""), summary))
    return turns[-CONTEXT_MAX_TURNS:]


def _render_context_history(turns: list[tuple[str, str]]) -> str:
    """Renders the turns as orientation text, capped at CONTEXT_MAX_CHARS.
    Empty string if there are no turns (first task of a session)."""
    if not turns:
        return ""
    lines = ["Previous history of this session (short orientation, not necessarily "
             "all relevant to the current task):", ""]
    for user_msg, summary in turns:
        lines.append(f"User (earlier): {user_msg}")
        lines.append(f"Result: {summary}")
        lines.append("")
    history_text = "\n".join(lines)
    if len(history_text) > CONTEXT_MAX_CHARS:
        history_text = "[...truncated...]\n" + history_text[-CONTEXT_MAX_CHARS:]
    return history_text


def _build_task_text(session: dict, message: str) -> str:
    """Builds the task text from the limited context (see _collect_context_turns/
    _render_context_history) plus the current message. Written ONLY into the
    task slot (what the model sees) -- the persisted message_received/
    session["messages"] history stays the ORIGINAL, short message, otherwise the
    context would inflate itself recursively with every follow-up turn."""
    history_text = _render_context_history(_collect_context_turns(session))
    if not history_text:
        return message
    return f"{history_text}--- Current task ---\n{message}"


def _start_stage(session_id: str, body: MessageIn,
                  events: BoundedSseQueue) -> tuple[StageContext, dict]:
    """P5 start phase -- the former _run_stage logic UP TO the wait and
    finalization phase, unchanged 1:1: task temp file, kill job, subprocess,
    LiveStage registry, stage_record + JSONL events (message_received,
    stage_started), pipe drainers, watchdog. Called by the POST /messages handler
    UNDER the session lock (404/409 check beforehand). Returns the context for
    the finalization phase plus the stage_started data object (pushed as the
    first SSE event). `events`: the stderr drainer pushes every log line
    immediately as a 'log_line' event (live visibility, e.g. whether/when
    run_substage runs -- user request after an unnoticed hang)."""
    stage_id = str(uuid.uuid4())
    started_at = _utcnow()
    start_mono = time.monotonic()
    deadline = start_mono + STAGE_TIMEOUT_S
    session = _sessions[session_id]

    # Task text as a UTF-8 temp file: --task @file bypasses the Windows argv limit (P3).
    # Follow-up package "limited context": the task slot no longer gets ONLY the
    # current message, but additionally the last CONTEXT_MAX_TURNS completed turns
    # of this session as a short orientation (_build_task_text).
    fd, task_path = tempfile.mkstemp(prefix=f"{stage_id}-", suffix=".txt", dir=TASK_TMP_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_build_task_text(session, body.message))

    # P4 + P1 fix "atomic job assignment": create the job, start the process
    # SUSPENDED, assign it, ONLY THEN resume it. Previously there was a time window
    # between Popen() (process already running) and _assign_process_to_job() in
    # which the stage runner could theoretically already have started a child
    # process (e.g. pwsh via run_powershell) BEFORE the job object was active --
    # that child would then not have been covered by the KILL_ON_JOB_CLOSE
    # protection. A process started suspended by design cannot execute any code
    # at all, and therefore cannot start child processes, until we explicitly
    # resume it via _resume_all_threads -- the window is thus structurally closed,
    # not just shortened. CREATE_SUSPENDED + the later resume via
    # Toolhelp32Snapshot/OpenThread instead of a Popen-owned thread handle,
    # because Popen closes that handle internally right away (see the comment at
    # CREATE_SUSPENDED further up) -- no private CPython internals, only the
    # public proc.pid plus documented Win32 APIs.
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
            creationflags=CREATE_NO_WINDOW | CREATE_SUSPENDED,
            # Environment inheritance (clarified): os.environ IS inherited
            # COMPLETELY (not just a subset) -- the stage runner needs among other
            # things PATH (for pwsh/git/etc. in run_powershell) and the variables
            # set by the system Python; there are no secrets here that the API
            # itself would not already have (single-user local system, no
            # multi-tenant separation needed). PYTHONIOENCODING is additionally
            # ENFORCED (overrides any other inherited encoding): without it,
            # Python on Windows writes to a non-console stream in the system code
            # page (here 850, not UTF-8) -- main.py decodes stdout/stderr strictly
            # as UTF-8, so umlauts etc. became "?" (found: "Kl?rung" instead of
            # "Klaerung" in the GUI's stderr_tail).
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        _assign_process_to_job(job_handle, proc)
        resumed = _resume_all_threads(proc.pid)
        if resumed == 0:
            # No thread found/resumable -- the process would stay frozen forever
            # (suspended, never running). Treat as a start error, identical
            # cleanup as for a failed assign.
            raise OSError(f"ResumeThread: no thread of PID {proc.pid} could be resumed")
    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()  # also works on a never-resumed (suspended) process
            except OSError:
                pass
        if job_handle is not None:
            _close_job_handle(job_handle)  # kills remaining tree members along with it
        _delete_quiet(task_path)
        log.error("Stage start failed: %s", exc)
        raise HTTPException(status_code=500, detail="stage subprocess could not be started")

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
    summary_capture = _SummaryCapture()
    log.info("Stage %s started (session %s, pid=%d, model=%s)",
             stage_id, session_id, proc.pid, body.model)
    try:
        session["stages"].append(stage_record)
        msg_event = _new_event("message_received", {
            "role": "user", "content": body.message, "stage_id": stage_id})
        _append_event(session_id, msg_event)
        # The in-memory state of the RUNNING server must see the same message as a
        # reconstruction after restart (_apply_event) -- otherwise GET /sessions/{id}
        # shows empty "messages" at runtime even though the JSONL file is correct
        # (bug found during the GUI test: reload showed an empty history despite a correct file).
        session["messages"].append({
            "role": "user", "content": body.message,
            "stage_id": stage_id, "ts": msg_event["ts"]})
        _append_event(session_id, _new_event("stage_started", {
            "stage_id": stage_id, "exit_code": None, "pid": proc.pid,
            "model": body.model, "project": body.project, "started_at": started_at}))

        # Push SSE stage_started BEFORE the drainers -- guarantees that the client
        # sees this event before any possible log_line (the drainers could
        # otherwise read something right after the start).
        started_data = {
            "stage_id": stage_id, "status": "running", "exit_code": None,
            "pid": proc.pid, "model": body.model, "project": body.project,
            "started_at": started_at,
        }
        events.put_priority(("stage_started", started_data))

        # Drain stdout/stderr CONTINUOUSLY (full pipe buffer = deadlock), keep tails.
        drainers = [
            threading.Thread(target=_drain_capped, args=(proc.stdout, out_chunks, TAIL_LIMIT), daemon=True),
            threading.Thread(target=_drain_capped,
                              args=(proc.stderr, err_chunks, TAIL_LIMIT, events, summary_capture),
                              daemon=True),
        ]
        for t in drainers:
            t.start()

        # P4: watchdog thread -- actively kills the job after STAGE_TIMEOUT_S.
        watchdog = threading.Thread(
            target=_watchdog_thread, args=(session_id, live, start_mono), daemon=True)
        watchdog.start()
    except Exception:
        # Start phase incomplete (extremely unlikely): same cleanup and persistence
        # rules as the error path of the finalization phase (race fix P1:
        # _kill_intent-based decision instead of only abort_event, handle closing
        # under the same lock as a possible kill attempt).
        log.exception("Start phase of stage %s (session %s) failed",
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
            "summary": "(API error during the start phase)",
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
            log.exception("stage_completed after start error could not be persisted (session %s)",
                          session_id)
        _delete_quiet(task_path)
        live.done_event.set()
        raise

    return StageContext(
        session_id=session_id, body=body, stage_id=stage_id, proc=proc,
        job_handle=job_handle, live=live, task_path=task_path,
        start_mono=start_mono, deadline=deadline, started_at=started_at,
        out_chunks=out_chunks, err_chunks=err_chunks, drainers=drainers,
        summary_capture=summary_capture,
    ), started_data


def _finalize_stage(ctx: StageContext) -> dict:
    """P5 finalization phase -- the former _run_stage logic FROM the wait and
    finalization phase on, unchanged 1:1 (exit codes 0-3 are the only status
    source; rule A for aborts; persisting the stage_completed event; registry
    cleanup; closing the job handle; done_event). Runs in its own worker thread
    WITHOUT the session lock. Returns the full stage object (pushed to the client
    as the terminal SSE event)."""
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

        # LINEARIZATION POINT (point C): rc is now known -- the process has ended,
        # one way or another. The question "has someone already registered an
        # abort before" is decided bindingly HERE, under _live_guard, ONCE AND
        # ONLY ONCE -- the same lock that _terminate_live_stage uses for its
        # check/registration makes both sides mutually exclusive: whoever gets
        # the lock first wins.
        # - _kill_intent still empty (None): WE are first -- the natural end wins
        #   bindingly, mark with _NATURAL so that an abort still arriving now is
        #   too late and changes nothing.
        # - _kill_intent already holds an abort reason: it was registered BEFORE
        #   this point -- rule A, the abort wins (take over the status).
        # _kill_intent is deliberately NOT removed here yet (see below) --
        # otherwise an abort coming through at exactly this moment could wrongly
        # believe it is first, because the registry entry (_live_stages) is only
        # removed later (after persistence).
        with _live_guard:
            existing = _kill_intent.get(session_id)
            if existing is None:
                _kill_intent[session_id] = _NATURAL
                aborted = False
                status = None
            else:
                aborted = existing != _NATURAL  # _NATURAL can never occur here, defensive
                status = existing if aborted else None

        for t in drainers:
            t.join(timeout=5)
        stdout_tail = b"".join(out_chunks).decode("utf-8", errors="replace")
        stderr_tail = b"".join(err_chunks).decode("utf-8", errors="replace")
        _delete_quiet(task_path)

        finished_at = _utcnow()
        duration_s = round(time.monotonic() - start_mono, 1)

        if aborted:
            # Rule A: the abort request wins; exit_code stays null (no runner verdict).
            status = status or STATUS_ABORTED
            exit_code = None
            summary = {
                STATUS_ABORTED: "Stage aborted by the user (POST /abort), no runner verdict.",
                STATUS_WATCHDOG: f"API watchdog terminated the stage after {STAGE_TIMEOUT_S // 60} min, no runner verdict.",
            }.get(status, "Stage actively terminated, no runner verdict.")
        else:
            exit_code = rc
            status = EXIT_OUTCOMES.get(rc, f"unknown_rc_{rc}")
            summary = _extract_summary(rc, stderr_tail, ctx.summary_capture.text)

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
            # Race fix (P1, point A): closing the handle now runs under THE SAME
            # lock as any possible kill attempt (_kill_job_locked) -- the
            # job_closed flag makes a later TerminateJobObject on this handle
            # impossible, no matter which thread comes next.
            _close_job_handle_locked(live)
            _live_stages.pop(session_id, None)
            _kill_intent.pop(session_id, None)  # only now: remove _NATURAL or the abort reason
        live.done_event.set()
        log.info("Stage %s finished: status=%s rc=%s after %.1f s",
                 stage_id, status, exit_code, duration_s)
        return {"session_id": session_id, "stage": stage_record}
    except Exception:
        log.exception("Finalization of stage %s (session %s) failed",
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
            "summary": "(API error during finalization)",
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
            log.exception("stage_completed after error could not be persisted (session %s)", session_id)
        _delete_quiet(task_path)
        live.done_event.set()
        raise


def _stage_worker(ctx: StageContext, events: BoundedSseQueue) -> None:
    """P5 worker thread (one per POST /messages): finalizes the stage started in
    the start phase and delivers the result as queue items to the SSE generator
    -- exactly ONE terminal event, then the sentinel. Runs WITHOUT the session
    lock; a client disconnect does not affect the runner (the stage keeps
    running, finalization/persistence unchanged -- aborting is still
    POST /abort). Terminal event and sentinel are priority puts (BoundedSseQueue)
    -- they are guaranteed to arrive, even if the queue was temporarily full of
    log_line events (those are evicted if necessary)."""
    try:
        result = _finalize_stage(ctx)
    except Exception:
        # _finalize_stage has already cleaned up registry/job/done_event and
        # persisted the stage_completed event; the stage object in the session
        # state reflects the final state -- deliver it as the terminal event.
        log.exception("Stage worker: unexpected error in stage %s", ctx.stage_id)
        result = {
            "session_id": ctx.session_id,
            "stage": next((s for s in _sessions[ctx.session_id]["stages"]
                           if s.get("stage_id") == ctx.stage_id), None) or {},
        }
    # Report dropped log_line events (overload) BEFORE the terminal event -- one
    # compact summary event instead of a warning per line; purely informative about
    # lost LIVE visibility, never about the (always complete) stage result.
    dropped = events.pop_dropped()
    if dropped:
        events.put_priority(("log_dropped", {"count": dropped}))
    events.put_priority(("stage_completed", result["stage"]))
    events.put_priority(None)  # sentinel: end of stream


async def _sse_stream(events: BoundedSseQueue, started_mono: float) -> AsyncIterator[str]:
    """P5 consumer side of the SSE stream: emits the pushed events in standard SSE
    format (event/data/frame end) and, as long as NO event arrives (stage is
    running), sends a heartbeat event every HEARTBEAT_INTERVAL_S. Ends with the
    sentinel, which is set after exactly ONE terminal event. A client disconnect
    during the stage does not affect the runner (deliberately: aborting is still
    POST /abort); the generator is then simply no longer consumed."""
    while True:
        try:
            item = await asyncio.to_thread(events.get, timeout=HEARTBEAT_INTERVAL_S)
        except queue.Empty:
            yield _sse_frame("heartbeat", {
                "elapsed_s": round(time.monotonic() - started_mono, 1)})
            continue
        if item is None:  # sentinel after the terminal event
            break
        etype, data = item
        yield _sse_frame(etype, data)


@app.post("/sessions/{session_id}/messages")
def post_message(session_id: str, body: MessageIn) -> StreamingResponse:
    """Runs the stage for the user's task and delivers the progress as an SSE
    stream (P5): content type text/event-stream, a stage_started event
    immediately, a heartbeat event every 5 s, and at the end exactly ONE terminal
    event stage_completed with the full stage object (status/exit_code/
    finished_at/duration_s/summary/stdout_tail/stderr_tail). No token streaming,
    no tool events (deliberately deferred, target architecture v1).

    The 404/409 check stays BEFORE the stream starts (fast path + recheck under
    the session lock, unchanged); the start phase also runs synchronously under
    the lock, so a competing POST can never start two stages of the same
    session. Afterwards a worker thread waits/finalizes; the response is returned
    immediately as a StreamingResponse."""
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
    # stage_started is already pushed INSIDE _start_stage (before the drainer
    # threads, see there) -- guarantees ordering before any log_line.
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
    """Aborts the running stage in a controlled way (P4): set kill intent +
    abort_event, kill the job object (whole process tree), wait for the worker's
    finalization (done_event, max. ABORT_FINALIZE_TIMEOUT_S). Unknown session ->
    404; known session WITHOUT a running stage -> 409 (rationale: session
    existence is the check with its own 404; 'no active stage' is a state
    conflict of the request, not a missing resource -- like the 409 for a
    duplicate POST)."""
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    live = _terminate_live_stage(session_id, STATUS_ABORTED)
    if live is None:
        raise HTTPException(status_code=409, detail="no running stage for this session")
    if not live.done_event.wait(timeout=ABORT_FINALIZE_TIMEOUT_S):
        raise HTTPException(status_code=500, detail="stage was not finalized after the kill")
    stage = next((s for s in session["stages"] if s.get("stage_id") == live.stage_id), None)
    return {"session_id": session_id, "stage": stage}
