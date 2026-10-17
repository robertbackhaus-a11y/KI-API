<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Architecture

Status: version 0.2.0 + integrated stage runner. Target architecture v1 (see the
coding-v2 decision_record).

## Components

    Browser (index.html) --HTTP/SSE--> FastAPI (reaper_ai.main, uvicorn, 1 process)
                                           |
                                           | one subprocess per stage:
                                           | python -P -m reaper_ai.runner.stage_runner
                                           v
                                stage runner (role=main) --HTTP /api/chat--> Ollama
                                           |
                                           +--> pwsh child processes, Basic Memory, web
                                           +--> run_substage: worker subprocess
                                                (same file, role=worker) --> Ollama

- **GUI:** a static page that only talks to the public endpoints
  ([api.md](api.md), [gui.md](gui.md)).
- **API:** FastAPI app in `src/reaper_ai/main.py`. Keeps the session state in
  memory, persists it as JSONL and starts stages.
- **Stage runner:** `src/reaper_ai/runner/stage_runner.py` is the sole AI
  orchestrator (Ollama tool loop, `finish_stage`, `run_substage`, Basic Memory,
  web and PowerShell tools, B07 context window management). It ships with the
  package (taken over from the former standalone `C:\tools\stage-runner` in its
  B07 version, functionally unchanged) but the API never imports it: every stage
  is a separate subprocess `<python> -P -m reaper_ai.runner.stage_runner` with the
  API's interpreter, the workspace directory (`<data-dir>/workspace`, not the data
  directory itself) as working directory and the package parent prepended to
  `PYTHONPATH`. `run_substage` starts a worker stage as a
  further subprocess of the same file (role fixed to `worker`, no further
  delegation). An external runner script can still be configured
  (`REAPER_STAGE_RUNNER_SCRIPT`, tests and rollback only).
- **Ollama:** the only model source. Main model `qwen3.8-q5:latest` (role=main,
  full REAPER persona); `qwen3:8b` is the fast model for subtasks delegated via
  `run_substage`. The runner reads the base URL from `REAPER_OLLAMA_URL`
  (inherited from the API), default `http://localhost:11434`.

### Context window management (B07, in the runner)

- `num_ctx` is sent explicitly with every Ollama request (`REAPER_NUM_CTX` /
  `--num-ctx`, default 65536, never raised automatically).
- Before every request the history is bounded deterministically: estimated tokens
  (conservative 3 characters per token) must stay below 90 % of `num_ctx` minus a
  response reserve and the tool schemas. The oldest tool results are replaced by a
  placeholder first; system prompt, task and the last 6 messages are never touched,
  and no message is deleted (tool call/result pairs stay intact).
- Tool results are capped to `REAPER_SHELL_OUTPUT_LIMIT` (default 8000) characters,
  keeping head and tail with an explicit truncation marker; `REAPER_DIAG_DIR`
  optionally keeps the full output on disk.
- HTTP errors from Ollama are logged with their body. Only a confirmed context
  error triggers exactly one controlled reduction (tail protection 2) and one
  retry; other errors end the stage with rc=1.
- Every iteration logs a compact `diag` line (prompt/response tokens, history
  size, estimate, compactions); it appears in the GUI live log.

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
  `TerminateJobObject` terminates the whole tree (runner, `run_substage` worker
  and all pwsh children, including ones started later). If the API itself dies,
  the OS closes the handles and terminates the trees; no orphan processes remain.
- **PowerShell containment (R08, fixed 2026-10-17):** job-object containment of
  pwsh children only works for a **non-packaged** (regular Win32) PowerShell,
  because a normal child process inherits its parent's job automatically. The
  Microsoft Store / MSIX build of PowerShell 7 does **not** inherit the job:
  packaged-app activation launches the real process into its own
  package-managed job outside ours, so `TerminateJobObject` never reaches it and
  the pwsh (plus anything it spawned) survives abort/watchdog. Verified at the
  process-tree level, this affects the packaged build reached **either** via the
  per-user execution alias (`%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe`)
  **or** via the direct package path
  (`C:\Program Files\WindowsApps\Microsoft.PowerShell_*\pwsh.exe`) — it is a
  property of the *packaged* build, not of the alias. A regular, non-packaged
  PowerShell 7 is therefore a hard requirement. `config.resolve_powershell()`
  returns `REAPER_PWSH` (only if it points at a non-packaged executable), else the
  regular PowerShell 7 (`C:\Program Files\PowerShell\7\pwsh.exe` or the per-user
  `%LOCALAPPDATA%\Programs\PowerShell\7\pwsh.exe`), else `""`. There is **no**
  fallback to the packaged build and **no** fallback to Windows PowerShell 5.1: if
  nothing suitable is found, `tool_run_powershell` returns a clear error and starts
  no shell, and `reaper-ai run` / the API lifespan log a warning with the install
  command. Nothing is installed automatically
  (`winget install --id Microsoft.PowerShell`, machine MSI via `--scope machine`
  from an elevated shell). The resolved executable is logged at every stage start.
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
      runner/__init__.py        (empty package marker)
      runner/stage_runner.py    stage runner, started only as a subprocess (python -m)
      web/index.html            chat GUI
      web/vendor/               marked, DOMPurify, highlight.js (+ license texts)
    run.ps1, stop.ps1, Start-Reaper.cmd   start/stop in checkout operation

Code and GUI live in the package, runtime data in the data directory outside the
package (see [operations.md](operations.md)). The package is installed directly
from GitHub (`pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git[@<tag>]`),
so everything under `src/reaper_ai/` that the installation needs must be tracked
in Git. `main.py` in the package reads only
environment variables; the CLI resolves options and `config.toml` into them
beforehand.

The shim `main.py` in the repository root sets `REAPER_AI_HOME` to the checkout
directory (if not set) and replaces itself with `reaper_ai.main`. `import main`
and `reaper_ai.main` are therefore the same module object, so local regression
tests can patch module globals.
