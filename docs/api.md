<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# HTTP API

Status: version 0.2.0. Source: `src/reaper_ai/main.py`.

Local single-user system, loopback only, no authentication. No OpenAPI schema, no
`/docs` or `/redoc` page (deliberately disabled). Default address:
`http://127.0.0.1:8734`.

## Overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Chat GUI (`src/reaper_ai/web/index.html`), see [gui.md](gui.md) |
| GET | `/vendor/<file>` | locally bundled GUI libraries (static) |
| GET | `/health` | diagnostics for the GUI status light |
| POST | `/sessions` | create a new session |
| GET | `/sessions/{id}` | read a session |
| GET | `/sessions/{id}/context` | preview of the limited context for the next message |
| POST | `/sessions/{id}/messages` | run a stage, progress as an SSE stream |
| POST | `/sessions/{id}/abort` | abort the running stage |

## GET /health

    {"api": "ok", "ollama": "ok" | "down", "stage_runner": "ok" | "missing"}

Purely diagnostic. Ollama is only checked via a TCP connect to `ollama_url` (see
[operations.md](operations.md)), the runner only for the presence of Python and
script. The API never calls Ollama itself; only `stage_runner.py` does.

## POST /sessions

Response `201` with the session object. `session_id` is a UUID v4. Creates
`<data-dir>/sessions/<session_id>.jsonl` with the event `session_created`.

## GET /sessions/{id}

Response `200`:

    {
      "session_id": "...",
      "created_at": "...",
      "messages": [{"role": "user", "content": "...", "stage_id": "...", "ts": "..."}],
      "stages":   [<stage object>, ...]
    }

`404` for an unknown id. `messages[].content` is always the original message,
never the task text extended with context.

## GET /sessions/{id}/context

Shows which context a next message would get prepended. Starts no stage. `404` for
an unknown id.

    {"turns_included": 3, "max_turns": 5, "max_chars": 6000, "preview": "Previous history ..."}

Limited context: besides the current message, the task text contains the last
5 completed turns of the session (user message plus stage summary), capped at
6000 characters (`CONTEXT_MAX_TURNS`, `CONTEXT_MAX_CHARS`). No full history;
durable knowledge remains the model's job via `memory_write`.

## POST /sessions/{id}/messages

Request body (JSON):

    {
      "message": "<user task text>",     # required, not empty and not whitespace only
      "model":   "qwen3.8-q5:latest",    # optional, default: Reaper's main model
      "project": "coding-v2",            # optional, default: Basic Memory project
      "think":   false                   # optional, default false (reasoning off)
    }

`think` is always passed to the runner explicitly as `--think on|off`. Without
this switch the runner would use `auto` and Qwen3/Q5 would think practically
without limit.

Errors before the stream starts: `404` unknown session, `409` a stage is already
running in this session (one stage per session at a time), `422` invalid body.

Response `200`, `Content-Type: text/event-stream`, standard SSE frames
(`event: <type>`, `data:` one JSON line, blank line as frame end). The events
arrive while the stage runs:

    event: stage_started            # right after the start phase, always first
    data: {"stage_id": "...", "status": "running", "exit_code": null, "pid": 12345,
           "model": "qwen3.8-q5:latest", "project": "coding-v2", "started_at": "..."}

    event: log_line                 # live, one per complete stderr line of the runner
    data: {"text": "[12:00:01] TOOL ..."}

    event: heartbeat                # every 5 s without another event
    data: {"elapsed_s": 5.0}

    event: log_dropped              # only on overload, once right before stage_completed
    data: {"count": 42}

    event: stage_completed          # exactly ONE terminal event with the full stage object
    data: {"stage_id": "...", "status": "success", "exit_code": 0, "pid": 12345,
           "model": "...", "project": "...", "started_at": "...", "finished_at": "...",
           "duration_s": 12.3, "summary": "<finish_stage summary>",
           "stdout_tail": "", "stderr_tail": "<last ~8 KB>"}

- `log_line` is raw log text and best effort: the event queue per stage has 1000
  slots; with a slow or disconnected client, log lines are dropped and counted
  (`log_dropped`). `stage_started` and `stage_completed` always arrive.
  Individual lines are capped at 64 KB (suffix ` ...[line truncated]`).
- No token streaming and no structured tool events (target architecture v1).
- A client disconnect does not abort the stage. Abort only via `/abort`.

Example:

    curl.exe -N -i -X POST http://127.0.0.1:8734/sessions/<id>/messages ^
      -H "Content-Type: application/json" ^
      -d "{\"message\": \"Say hello briefly and then call finish_stage\"}"

## POST /sessions/{id}/abort

Terminates the process tree of the running stage and waits for its finalization.
Response `200`: `{"session_id": "...", "stage": <stage object with status "aborted">}`.
`404` unknown session, `409` no running stage, `500` if the stage is not finalized
30 s after the kill.

## Error messages

Error responses use FastAPI's `{"detail": "..."}` format with English texts:
`unknown session_id` (404), `stage already running for this session` (409),
`no running stage for this session` (409), `stage subprocess could not be started`
(500), `stage was not finalized after the kill` (500).

## Stage status

| status | exit_code | Meaning |
|---|---|---|
| `running` | null | stage is running |
| `success` | 0 | runner succeeded |
| `error` | 1 | runner error or `success=false` |
| `wall_clock` | 2 | runner's internal 20 min emergency brake |
| `max_iterations` | 3 | runner's iteration limit |
| `aborted` | null | user abort via `/abort` |
| `watchdog_timeout` | null | the API watchdog (25 min) terminated the stage |
| `incomplete` | null | stage was still running when the API restarted; it is not re-run |
| `unknown_rc_N` | N | defensive, should not occur |

The status comes exclusively from the exit code. `summary` is pure display text:
the finish_stage summary after the `=== STAGE BEENDET` marker (written by
`stage_runner.py`). If no summary is available, the API fills in an English
fallback text (e.g. `Aborted: wall-clock limit (20 min) reached, no finish_stage
call.`).

## Testing

    curl.exe -i -X POST http://127.0.0.1:8734/sessions
    curl.exe -i http://127.0.0.1:8734/sessions/<session_id>
    curl.exe -i http://127.0.0.1:8734/sessions/no-such-id  # expected: 404
    curl.exe -i http://127.0.0.1:8734/health
