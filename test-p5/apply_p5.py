# -*- coding: utf-8 -*-
"""P5-Einschub: SSE-Statusausgabe fuer POST /sessions/{id}/messages."""
from pathlib import Path

p = Path(r"C:\tools\reaper-api\main.py")
src = p.read_text(encoding="utf-8")
orig = src


def replace_once(old: str, new: str) -> None:
    global src
    n = src.count(old)
    assert n == 1, f"Anker nicht eindeutig ({n}x): {old[:90]!r}"
    src = src.replace(old, new)


replace_once(
    "import json\nimport logging\nimport os\nimport re\nimport subprocess\nimport sys\n"
    "import tempfile\nimport threading\nimport time\nimport uuid\n"
    "from contextlib import asynccontextmanager\n"
    "from datetime import datetime, timezone\n"
    "from pathlib import Path\n\n"
    "from fastapi import FastAPI, HTTPException\n"
    "from pydantic import BaseModel, Field, field_validator\n",
    "import asyncio\n"
    "import json\nimport logging\nimport os\nimport queue\nimport re\nimport subprocess\nimport sys\n"
    "import tempfile\nimport threading\nimport time\nimport uuid\n"
    "from collections.abc import AsyncIterator\n"
    "from contextlib import asynccontextmanager\n"
    "from dataclasses import dataclass, field\n"
    "from datetime import datetime, timezone\n"
    "from pathlib import Path\n\n"
    "from fastapi import FastAPI, HTTPException\n"
    "from pydantic import BaseModel, Field, field_validator\n"
    "from starlette.responses import StreamingResponse\n",
)

replace_once(
    '"""Reaper API -- P4: HTTP-Geruest + Sessions + JSONL-Persistenz + Stage als Subprozess\n'
    "+ Fehler-/Abbruchbehandlung (Prozessbaum-Kill, aktiver Watchdog, POST /abort).\n",
    '"""Reaper API -- P5: HTTP-Geruest + Sessions + JSONL-Persistenz + Stage als Subprozess\n'
    "+ Fehler-/Abbruchbehandlung (Prozessbaum-Kill, aktiver Watchdog, POST /abort)\n"
    "+ SSE-Statusausgabe fuer POST /sessions/{id}/messages.\n",
)

replace_once(
    "- POST /sessions/{id}/messages    Stage fuer den Nutzer-Task SYNCHRON ausfuehren\n"
    "                                  (blockiert bis der Subprozess fertig ist;\n"
    "                                  kein Streaming/SSE -- das ist P5)\n",
    "- POST /sessions/{id}/messages    Stage fuer den Nutzer-Task ausfuehren und den\n"
    "                                  Fortschritt als SSE-Stream liefern (P5):\n"
    "                                  text/event-stream, sofort ein stage_started-\n"
    "                                  Event, alle 5 s ein heartbeat-Event, am Ende\n"
    "                                  genau EIN Terminal-Event stage_completed mit\n"
    "                                  dem vollen Stage-Objekt (status/exit_code/\n"
    "                                  finished_at/duration_s/summary/stdout_tail/\n"
    "                                  stderr_tail). 404/409 bleiben VOR dem\n"
    "                                  Stream-Start. Kein Token-Streaming, keine\n"
    "                                  Tool-Events (bewusst, Zielarchitektur v1).\n",
)

replace_once(
    "Nicht in P4 (bewusst vertagt): SSE-Statusausgabe (P5), Begrenzter Chat-Kontext fuer\n"
    "Folgeauftraege (der Task-Slot erhaelt vorerst die Nachricht unveraendert).\n"
    '"""\n',
    "P5-Ablauf: POST /messages laeuft die Startphase UNTER dem Session-Lock synchron\n"
    "durch (404/409-Pruefung bleibt VOR dem Stream-Start; Subprozess + Kill-Job +\n"
    "Drainer + Watchdog + JSONL-Events message_received/stage_started) und gibt dann\n"
    "eine StreamingResponse zurueck. Warten auf den Exit, Finalisierung und\n"
    "Persistenz des Terminal-Events laufen in einem Worker-Thread pro Request und\n"
    "liefern das Ergebnis als Queue-Items an den async SSE-Generator (stage_started\n"
    "wird vorab im Request-Thread gepusht). Client-Trennung waehrend der Stage\n"
    "aendert nichts am Runner-Lauf -- Abbruch bleibt POST /abort.\n"
    "\n"
    "Nicht in P5 (bewusst vertagt): Begrenzter Chat-Kontext fuer Folgeauftraege (der\n"
    "Task-Slot erhaelt vorerst die Nachricht unveraendert).\n"
    '"""\n',
)

replace_once(
    "# Pro Pipe so viel Tail in Speicher/Event halten (der Runner schreibt praktisch nur stderr).\n"
    "TAIL_LIMIT = 8 * 1024\n",
    "# Pro Pipe so viel Tail in Speicher/Event halten (der Runner schreibt praktisch nur stderr).\n"
    "TAIL_LIMIT = 8 * 1024\n"
    "\n"
    "# P5: Herzschlag-Intervall des SSE-Streams (Stage laeuft sonst bis zu 20 min;\n"
    "# das Event haelt den Stream sichtbar live und beweist Fortschritt an den Client).\n"
    "HEARTBEAT_INTERVAL_S = 5.0\n",
)

replace_once('version="0.4.0-p4",', 'version="0.5.0-p5",')

lines = src.split("\n")
i_start = next(i for i, l in enumerate(lines) if l.startswith("def _run_stage("))
i_end = next(i for i, l in enumerate(lines) if l.startswith('@app.post("/sessions/{session_id}/abort")'))

new_block = '''@dataclass
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
    """Standard-SSE-Rahmen: 'event: <typ>\\ndata: <eine JSON-Zeile>\\n\\n'."""
    return f"event: {etype}\\ndata: {json.dumps(data, ensure_ascii=False)}\\n\\n"


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

    # Task-Text als UTF-8-Tempdatei: --task @datei umgeht das Windows-argv-Limit (P3).
    fd, task_path = tempfile.mkstemp(prefix=f"{stage_id}-", suffix=".txt", dir=TASK_TMP_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body.message)

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

    session = _sessions[session_id]
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
        _append_event(session_id, _new_event("message_received", {
            "role": "user", "content": body.message, "stage_id": stage_id}))
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


'''

lines[i_start:i_end] = new_block.split("\n")
src = "\n".join(lines)

if src == orig:
    raise SystemExit("NICHTS AENDERTE -- Abbruch")
p.write_text(src, encoding="utf-8")
print(f"OK: {len(orig)} -> {len(src)} Zeichen")
print(f"Splice-Zeilen {i_start}..{i_end} ersetzt")
