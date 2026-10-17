<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Architecture

Status: version 0.1.0. Target architecture v1 (see the coding-v2 decision_record
in the stage-runner folder).

## Components

    Browser (index.html) --HTTP/SSE--> FastAPI (reaper_ai.main, uvicorn, 1 process)
                                           |
                                           | one subprocess per stage
                                           v
                                stage_runner.py (own venv) --HTTP--> Ollama
                                           |
                                           +--> pwsh child processes, Basic Memory

- **GUI:** a static page that only talks to the public endpoints
  ([api.md](api.md), [gui.md](gui.md)).
- **API:** FastAPI app in `src/reaper_ai/main.py`. Keeps the session state in
  memory, persists it as JSONL and starts stages.
- **Stage runner:** `stage_runner.py` is the sole AI orchestrator. It is neither
  imported nor installed with the package, but started as an external subprocess
  with its own venv, and stays unchanged.
- **Ollama:** the only model source. Main model `qwen3.8-q5:latest` (role=main,
  full REAPER persona); `qwen3:8b` is the fast model for subtasks delegated via
  `run_substage`. The runner's Ollama URL is hard-coded there.

## Stage flow

1. **Start phase** (request thread, under the session lock): 404/409 check, build
   the task text with limited context and write it as a UTF-8 file to
   `<data-dir>/tasks/` (bypasses the Windows argv limit), create a job object,
   start the runner suspended (`CREATE_SUSPENDED | CREATE_NO_WINDOW`), assign it
   to the job, only then resume it.
   Invocation: `--model … --project … --think on|off --role main --task @<file>`.
   The events `message_received` and `stage_started` are persisted,
   `stage_started` goes into the queue as the first SSE event. Then the pipe
   drainers and the watchdog start.
2. **Pipe drainers:** stdout and stderr are drained continuously (full buffer =
   deadlock). A diagnostic tail of 8 KB per pipe is kept. Every stderr line goes
   live into the queue as `log_line` and into the capture of the finish_stage
   summary (from the `=== STAGE BEENDET` marker written by the runner, max. 64 KB),
   so the summary is not lost even with long logs.
3. **Worker thread** (one per request, no lock): waits for the process to end,
   finalizes (status from the exit code, summary, tails), appends
   `stage_completed` to the JSONL, deletes the task file and delivers the
   terminal event plus end marker to the SSE generator.
4. **SSE generator:** emits the queue entries and sends a `heartbeat` every 5 s
   as long as nothing else arrives. A client disconnect does not affect the
   runner.

## Bounded event queue

`BoundedSseQueue` has 1000 slots and two delivery classes. `log_line` never blocks
and is dropped and counted when the queue is full. Priority events
(`stage_started`, `log_dropped`, `stage_completed`, end marker) specifically evict
a `log_line` entry, never another priority event. This keeps memory per stage
bounded and the stderr drainer always running.

## Abort, watchdog, process tree

- Every stage runs in a Windows job object with `KILL_ON_JOB_CLOSE`.
  `TerminateJobObject` terminates the whole tree (runner and all pwsh children,
  including ones started later). If the API itself dies, the OS closes the
  handles and terminates the trees; no orphan processes remain.
- **POST /abort** sets status `aborted`, terminates the job and waits up to 30 s
  for finalization.
- **API watchdog:** 25 min (`REAPER_STAGE_TIMEOUT_S`), deliberately longer than the
  runner's internal 20 min emergency brake. Fires only on a real hang, status
  `watchdog_timeout`.

## Persistence and recovery

- One JSONL file per session under `<data-dir>/sessions/<session_id>.jsonl`,
  append-only, one line per event: `session_created`, `message_received`,
  `stage_started`, `stage_completed`. Status changes are appended, never
  overwritten.
- On server start all files are read and the state is reconstructed from the
  full event sequence. Invalid JSON lines are skipped with a warning, files
  without `session_created` are not loaded, unknown event types are ignored.
- Stages that are still `running` at start are marked `incomplete` and are not
  re-run.

## Package and directory layout

    pyproject.toml              package definition, console command reaper-ai
    main.py                     compatibility shim for checkout operation (not in the package)
    src/reaper_ai/
      __init__.py               __version__ (single version source)
      __main__.py               python -m reaper_ai
      cli.py                    run / stop / migrate / --version
      config.py                 data directory, config.toml, environment variables
      main.py                   FastAPI app, stage control, persistence
      web/index.html            chat GUI
      web/vendor/               marked, DOMPurify, highlight.js (+ license texts)
    run.ps1, stop.ps1, Start-Reaper.cmd   start/stop in checkout operation

Code and GUI live in the package, runtime data in the data directory outside the
package (see [operations.md](operations.md)). `main.py` in the package reads only
environment variables; the CLI resolves options and `config.toml` into them
beforehand.

The shim `main.py` in the repository root sets `REAPER_AI_HOME` to the checkout
directory (if not set) and replaces itself with `reaper_ai.main`. `import main`
and `reaper_ai.main` are therefore the same module object, so local regression
tests can patch module globals.
