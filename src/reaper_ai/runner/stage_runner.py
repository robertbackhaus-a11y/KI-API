#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later
"""
stage_runner -- one run = one stage, fresh context, explicit end.

Integrated into the Reaper-AI package (formerly the standalone
C:\\tools\\stage-runner\\stage_runner.py, taken over in its B07 version). The
runner is the sole agent orchestrator; the API (reaper_ai.main) never imports
this module but starts it as a separate subprocess:

  python -m reaper_ai.runner.stage_runner --model qwen3.8-q5:latest ^
      --project coding-v2 --role main --think off --task @task.txt

Principles:

  - ONE call = ONE stage with ONE fresh, empty context (no chat history, no
    inherited tool history).
  - The stage works with tools (shell, Basic Memory, web) until it calls the
    explicit tool `finish_stage` -- no guessing from text, no "just keep going
    until the limit hits".
  - Durable knowledge (facts, decisions, results) goes into the Basic Memory
    project, not into the context of the next stage.
  - role=main gets the additional tool run_substage, which starts a worker
    stage as its own subprocess (this same file, role fixed to "worker").

Backend: native Ollama endpoint /api/chat (base URL from REAPER_OLLAMA_URL,
default http://localhost:11434). Context window management (B07): num_ctx via
REAPER_NUM_CTX / --num-ctx, tool output limit via REAPER_SHELL_OUTPUT_LIMIT /
--shell-output-limit.

The protocol markers "=== STAGE BEENDET" and "run_substage GESTARTET:" as well
as the German system prompts/log texts are part of the runner's established
output format and model behaviour and are kept verbatim.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Basic Memory nutzt loguru und loggt standardmaessig recht geschwaetzig (DEBUG) auf
# stderr -- vor dem ersten Import von basic_memory setzen, damit es greift.
os.environ.setdefault("LOGURU_LEVEL", "WARNING")
from html.parser import HTMLParser
from typing import Any, Optional

import requests


# ---------------------------------------------------------------------------
# Environment-backed config helpers (B07). English only (project language policy).
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    """Read an int from the environment; fall back to `default` on unset/invalid."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _ollama_chat_url() -> str:
    """Native /api/chat endpoint. Configurable via REAPER_OLLAMA_URL (Reaper-AI's
    existing config name -- main.py forwards the server environment to the runner
    subprocess, so a base URL set there reaches us). The variable holds a base URL
    without a path; the native /api/chat endpoint is appended. Falls back to the
    previous hard-coded default when the variable is unset, so existing setups are
    unaffected. Primarily this also lets the B07 tests point the runner at a fake
    Ollama endpoint without touching the live model."""
    base = os.environ.get("REAPER_OLLAMA_URL")
    if base and base.strip():
        return base.strip().rstrip("/") + "/api/chat"
    return "http://localhost:11434/api/chat"


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
# Ollama native API -- think=false is only honoured here, not on the OpenAI-
# compatible /v1/chat/completions endpoint (verified: think/chat_template_kwargs
# are ignored there).
OLLAMA_URL = _ollama_chat_url()
SEARXNG_URL = "http://localhost/searxng/"
DEFAULT_MAX_CHARS = 2500
HARD_MAX_CHARS = 8000
SHELL_TIMEOUT_S = 120
# B07: previously a fixed 20_000. Default lowered to 8_000 (less context pressure
# per tool result) and made configurable via REAPER_SHELL_OUTPUT_LIMIT / the
# --shell-output-limit CLI flag. Truncation keeps head+tail and is marked
# explicitly (see _cap_output / tool_run_powershell).
SHELL_OUTPUT_LIMIT = _env_int("REAPER_SHELL_OUTPUT_LIMIT", 8_000)
MAX_ITERATIONS = 40
WALL_CLOCK_LIMIT_S = 20 * 60  # 20 Minuten Notbremse pro Stage

# ---------------------------------------------------------------------------
# Context window management (B07)
# ---------------------------------------------------------------------------
# num_ctx passed explicitly to Ollama's request options. Default 65_536 (the
# value the live model currently runs with); configurable via REAPER_NUM_CTX /
# --num-ctx. We never raise it automatically to 131_072 / 262_144.
DEFAULT_NUM_CTX = _env_int("REAPER_NUM_CTX", 65_536)
# Conversation history is kept below SAFETY_MARGIN * num_ctx minus headroom for
# the next response and the (fixed) system prompt + tool schemas. We estimate
# tokens from characters with a deliberately LOW chars-per-token ratio, i.e. we
# OVER-estimate token counts -- a conservative safety margin, never treating
# character counts as exact token counts.
CONTEXT_SAFETY_MARGIN = 0.90
CONTEXT_RESPONSE_RESERVE_TOKENS = _env_int("REAPER_CTX_RESPONSE_RESERVE", 4_096)
CHARS_PER_TOKEN = 3.0  # conservative; real BPE for qwen/latin text is ~3.5-4.0
# Most-recent messages never compacted (preserve recent conversational context).
CONTEXT_PROTECT_TAIL = 6
# Optional: when REAPER_DIAG_DIR is set, full (un-truncated) tool outputs are
# written there for post-hoc diagnosis. Off by default to avoid writing secrets
# to disk unless the operator opts in.
DIAG_DIR = os.environ.get("REAPER_DIAG_DIR") or ""

# ---------------------------------------------------------------------------
# PowerShell executable resolution (R08: process containment)
# ---------------------------------------------------------------------------
# The API (reaper_ai.main) runs every stage inside a Windows job object with
# KILL_ON_JOB_CLOSE, so abort/watchdog (TerminateJobObject) kills the whole
# process tree, including pwsh children started by run_powershell. This only
# works for a NON-PACKAGED (regular Win32) PowerShell, because a normal child
# process inherits its parent's job automatically.
#
# The Microsoft Store / MSIX build of PowerShell 7 -- reached either via the
# per-user WindowsApps execution alias OR directly under
# C:\Program Files\WindowsApps\Microsoft.PowerShell_* -- does NOT inherit the job:
# packaged-app activation launches the real process into its own package-managed
# job outside ours, so TerminateJobObject never reaches it and the pwsh (plus
# anything it spawned) SURVIVES abort/watchdog. Verified at the process-tree
# level: BOTH the alias and the direct packaged exe break out; a regular Win32
# PowerShell is contained. The deciding property is "packaged", not "alias".
#
# Policy (see reaper_ai.config.resolve_powershell): a regular, non-packaged
# PowerShell 7 is a HARD requirement. There is NO silent fallback to the packaged
# build and NO silent fallback to Windows PowerShell 5.1. If none is found, PWSH
# is "" and tool_run_powershell refuses to launch a shell (clear error) rather
# than start an uncontained process. Nothing is ever auto-installed.
# Import the shared PowerShell resolver from reaper_ai.config. This file runs in
# two ways: as a package module (`python -P -m reaper_ai.runner.stage_runner`, the
# main stage -- relative import works) AND as a standalone script launched by path
# (the run_substage worker: `python <stage_runner.py> --role worker` -- then there
# is no package context, so the relative import fails and we add the package parent
# (`.../src`) to sys.path and import absolutely).
try:
    from ..config import (  # noqa: E402
        resolve_powershell as _resolve_powershell,
        is_packaged_pwsh as _is_packaged_pwsh,  # re-exported for tests
        powershell_install_hint as _powershell_install_hint,
    )
except ImportError:  # launched as a plain script (run_substage worker)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # .../src
    from reaper_ai.config import (  # noqa: E402
        resolve_powershell as _resolve_powershell,
        is_packaged_pwsh as _is_packaged_pwsh,
        powershell_install_hint as _powershell_install_hint,
    )

PWSH = _resolve_powershell()  # path of a non-packaged PowerShell 7, or "" if none
CREATE_NO_WINDOW = 0x08000000
_PROLOGUE = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "try {$OutputEncoding=[System.Text.Encoding]::UTF8} catch {};"
    "try {$PSNativeCommandUseErrorActionPreference='SilentlyContinue'} catch {};"
    "$ProgressPreference='SilentlyContinue';try {$PSStyle.OutputRendering='None'} catch {};"
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# B07 helpers: output capping, token estimation, context-budget compaction,
# Ollama HTTP error classification, optional diagnostic dump.
# ---------------------------------------------------------------------------
def _cap_output(text: str, limit: int) -> tuple[str, bool]:
    """Cap `text` to `limit` characters, keeping head AND tail and marking the
    cut explicitly so a reader (and the model) can see output was dropped. The
    explicit marker also means a structured result is never silently truncated
    into something that looks complete. Returns (text, truncated)."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = f"\n...[truncated: kept {limit} of {len(text)} chars]...\n"
    half = max(1, (limit - len(marker)) // 2)
    return text[:half] + marker + text[-half:], True


class OllamaHTTPError(RuntimeError):
    """Raised by call_ollama on an HTTP >=400 response. Carries the status code
    and the response body so the caller can log it and distinguish a confirmed
    context error from a generic failure (B07)."""

    def __init__(self, status_code: int, body_text: str) -> None:
        self.status_code = status_code
        self.body_text = body_text or ""
        super().__init__(f"Ollama HTTP {status_code}: {self.body_text[:500]}")


# Textual signals in an Ollama error body that clearly attribute the failure to
# the context/prompt length (NOT generic 500s, and NOT bare CUDA/OOM which may
# stem from model loading rather than history growth).
_CONTEXT_ERROR_RE = re.compile(
    r"context\s*(?:length|window|size)|num_ctx|n_ctx|exceed|too (?:long|large|many)|"
    r"maximum context|prompt is too|token limit",
    re.I,
)


def _is_context_error(exc: BaseException) -> bool:
    return isinstance(exc, OllamaHTTPError) and bool(_CONTEXT_ERROR_RE.search(exc.body_text))


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate from character count. A LOW chars-per-token
    ratio OVER-estimates tokens on purpose (safety margin). Never treat this as
    an exact token count -- it is only used to decide when to compact."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _message_tokens(msg: dict) -> int:
    total = _estimate_tokens(msg.get("content") or "")
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        total += _estimate_tokens(fn.get("name", "") or "")
        args = fn.get("arguments")
        total += _estimate_tokens(args if isinstance(args, str)
                                  else json.dumps(args, ensure_ascii=False))
    return total + 8  # small per-message framing overhead


def _history_tokens(messages: list[dict]) -> int:
    return sum(_message_tokens(m) for m in messages)


_COMPACTED_PLACEHOLDER = "[older tool output removed to stay within the context budget]"


def _context_budget(num_ctx: int, tools: list[dict]) -> int:
    """Token budget available for the conversation history: a safety fraction of
    num_ctx, minus headroom reserved for the next model response and for the
    (fixed) tool schemas sent on every request."""
    tools_tokens = _estimate_tokens(json.dumps(tools, ensure_ascii=False))
    budget = int(num_ctx * CONTEXT_SAFETY_MARGIN) - CONTEXT_RESPONSE_RESERVE_TOKENS - tools_tokens
    return max(budget, 1024)


def _compact_history(messages: list[dict], num_ctx: int, tools: list[dict],
                     protect_tail: int = CONTEXT_PROTECT_TAIL) -> tuple[int, int]:
    """Deterministically bound the conversation history: replace the content of
    the OLDEST 'tool' messages first with a short placeholder until the estimated
    history fits the budget. Messages are never deleted, only shrunk, so ordering
    and every assistant tool_call / tool result relationship stay intact. The
    system prompt (index 0), the task (index 1) and the most recent `protect_tail`
    messages are never touched. Returns (compacted_count, estimated_tokens_after)."""
    budget = _context_budget(num_ctx, tools)
    if _history_tokens(messages) <= budget:
        return 0, _history_tokens(messages)
    compacted = 0
    upper = max(2, len(messages) - protect_tail)
    for i in range(2, upper):
        if _history_tokens(messages) <= budget:
            break
        m = messages[i]
        content = m.get("content") or ""
        if m.get("role") == "tool" and content != _COMPACTED_PLACEHOLDER \
                and len(content) > len(_COMPACTED_PLACEHOLDER):
            m["content"] = _COMPACTED_PLACEHOLDER
            compacted += 1
    # If the history still exceeds the budget after compacting every eligible
    # older tool output, we stop here on purpose: the system prompt, task and the
    # protected recent tail are never sacrificed by normal compaction. Cutting
    # deeper into recent context happens ONLY as the sanctioned controlled retry
    # after a *confirmed* context error (run_stage calls this with protect_tail=2).
    remaining = _history_tokens(messages)
    if remaining > budget:
        _log(f"context budget not fully met after compaction: est_tokens={remaining} "
             f"budget={budget} (system/task/recent tail preserved)")
    return compacted, remaining


def _diag_dump(stage_tag: str, name: str, content: str) -> None:
    """When REAPER_DIAG_DIR is set, keep the complete (un-truncated) tool output
    on disk for later diagnosis. Opt-in only, to avoid writing possibly sensitive
    output to disk by default."""
    if not DIAG_DIR:
        return
    try:
        d = Path(DIAG_DIR)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{stage_tag}_{name}")[:120]
        (d / f"{safe}_{time.strftime('%H%M%S')}.txt").write_text(content, encoding="utf-8")
    except Exception as exc:  # diagnostics must never break a stage
        _log(f"diag dump failed: {exc}")


# ---------------------------------------------------------------------------
# Tool: run_powershell (Logik wie C:\tools\shell-mcp\mcp_shell_server.py)
# ---------------------------------------------------------------------------
def tool_run_powershell(command: str, workdir: Optional[str] = None,
                         timeout_seconds: int = SHELL_TIMEOUT_S) -> dict:
    # R08: refuse to launch anything if no non-packaged PowerShell 7 was found.
    # Never fall back to the packaged (Store/MSIX) build or to Windows PowerShell
    # 5.1 -- an uncontained shell could survive abort/watchdog. A clear error is
    # returned to the model/caller instead.
    if not PWSH:
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": "No suitable PowerShell found. " + _powershell_install_hint()}
    timeout_s = max(1, min(int(timeout_seconds or SHELL_TIMEOUT_S), 600))
    full_cmd = _PROLOGUE + command
    # Default working directory: an explicit workdir wins; otherwise the dedicated
    # workspace directory passed by the API as REAPER_WORKDIR (NOT the Reaper data
    # directory), so a relative path in a model command never lands among Reaper's
    # own sessions/tasks/config files. Falls back to the process cwd (None) only
    # when REAPER_WORKDIR is unset (e.g. the runner started standalone).
    effective_workdir = workdir or os.environ.get("REAPER_WORKDIR") or None
    try:
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-NoLogo", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", full_cmd],
            cwd=effective_workdir,
            capture_output=True, timeout=timeout_s,
            creationflags=CREATE_NO_WINDOW,
            # stdin=DEVNULL: root cause eines echten Haengers (2026-10-16, live beim
            # Nutzer aufgetreten) -- ein vom Modell abgesetztes `ssh ... root@...`
            # (OpenSSH, nicht das hier uebliche plink/.ppk) konnte den Key nicht
            # verwenden, fiel auf Passwort-Auth zurueck und wartete auf stdin, die es
            # nie bekam (geerbtes stdin ueber mehrere Prozessebenen, keine echte
            # Konsole) -- subprocess.run()s eigener timeout griff NICHT, weil die
            # communicate()-Reader-Threads auf EOF warteten, das nie kam. Ein
            # geschlossenes stdin laesst jedes interaktive Prompt (Passwort,
            # Host-Key-Bestaetigung, …) sofort auf EOF statt auf ewiges Warten laufen.
            stdin=subprocess.DEVNULL,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        # Cap stdout (head+tail, explicitly marked); stderr is left intact so
        # error messages are always preserved for the model (B07).
        out, truncated = _cap_output(out, SHELL_OUTPUT_LIMIT)
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": out, "stderr": err, "truncated": truncated}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None,
                "stdout": "", "stderr": f"[Timeout nach {timeout_s}s]"}
    except Exception as exc:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": str(exc)}


# ---------------------------------------------------------------------------
# Tool: web_search / web_fetch_page (Logik wie C:\tools\webfetch-mcp\server.py)
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "nav", "header", "footer", "svg", "form"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._chunks: list[str] = []
        self.title = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip > 0:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._in_title and self.title is None and data.strip():
            self.title = data.strip()
        if self._skip == 0:
            self._chunks.append(data)

    def get_text(self):
        text = "".join(self._chunks)
        text = re.sub(r"[ \t]+", " ", text)
        return re.sub(r"\n[ \t]*\n+", "\n\n", text).strip()


def tool_web_search(query: str, max_results: int = 5) -> str:
    try:
        r = requests.get(SEARXNG_URL.rstrip("/") + "/search",
                          params={"q": query, "format": "json"}, timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])[: max(1, min(max_results, 10))]
    except Exception as exc:
        return f"Fehler bei der Suche: {exc}"
    if not results:
        return f"Keine Treffer fuer: {query}"
    lines = []
    for res in results:
        snip = (res.get("content") or "").strip()
        if len(snip) > 400:
            snip = snip[:400] + "..."
        lines.append(f"- {res.get('title', '(ohne Titel)')}\n  {res.get('url', '')}\n  {snip}")
    return "\n".join(lines)


def tool_web_fetch_page(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    if not url.startswith(("http://", "https://")):
        return "Fehler: url muss mit http:// oder https:// beginnen."
    capped = max(500, min(max_chars, HARD_MAX_CHARS))
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        return f"Fehler beim Laden von {url}: {exc}"
    if "text/html" in r.headers.get("Content-Type", ""):
        p = _TextExtractor()
        try:
            p.feed(r.text)
        except Exception:
            pass
        text, title = p.get_text(), p.title
    else:
        text, title = r.text, None
    truncated = len(text) > capped
    body = text[:capped]
    header = f"Quelle: {url}"
    if title:
        header += f"\nTitel: {title}"
    if truncated:
        header += f"\n[GEKUERZT: {capped} von {len(text)} Zeichen]"
    return f"{header}\n\n{body}"


# ---------------------------------------------------------------------------
# Tool: Basic Memory (Logik wie C:\tools\memory-wrapper\server.py, synchron)
# ---------------------------------------------------------------------------
def _bm_project() -> str:
    return _BM_PROJECT


_BM_PROJECT = ""  # wird in main() aus --project gesetzt


_project_ids: dict[str, str] = {}


async def _resolve_project_id(project_client, project_name: str) -> str:
    if project_name not in _project_ids:
        resolved = await project_client.resolve_project(project_name)
        _project_ids[project_name] = resolved.external_id
    return _project_ids[project_name]


async def _bm_list_projects():
    from basic_memory.mcp.async_client import get_client
    from basic_memory.mcp.clients.project import ProjectClient
    async with get_client(project_name=_bm_project()) as http:
        pl = await ProjectClient(http).list_projects()
        lines = []
        for p in pl.projects:
            mark = " (default)" if p.name == pl.default_project else ""
            lines.append(f"- {p.name}{mark}")
        return "\n".join(lines) if lines else "Keine Projekte gefunden."


async def _bm_search(query: str, entity_type: str = "", category: str = "", limit: int = 10,
                      project: Optional[str] = None):
    from basic_memory.mcp.async_client import get_client
    from basic_memory.mcp.clients.search import SearchClient
    from basic_memory.mcp.clients.project import ProjectClient
    proj = project or _bm_project()
    async with get_client(project_name=proj) as http:
        pid = await _resolve_project_id(ProjectClient(http), proj)
        q: dict[str, Any] = {"text": query} if query else {}
        if entity_type:
            q["entity_types"] = [entity_type]
        if category:
            q["categories"] = [category]
        resp = await SearchClient(http, pid).search(q, page_size=limit)
        items = resp.results
        if not items:
            return "Keine Treffer."
        lines = []
        for r in items[:limit]:
            snippet = (getattr(r, "matched_chunk", None) or getattr(r, "content", None) or "").strip().replace("\n", " ")[:160]
            cat = f" category={r.category}" if getattr(r, "category", None) else ""
            lines.append(f"- {r.title} ({r.permalink}){cat} score={r.score:.2f}\n  {snippet}")
        return "\n".join(lines)


async def _bm_read(identifier: str, project: Optional[str] = None):
    from basic_memory.mcp.async_client import get_client
    from basic_memory.mcp.clients.knowledge import KnowledgeClient
    from basic_memory.mcp.clients.project import ProjectClient
    proj = project or _bm_project()
    async with get_client(project_name=proj) as http:
        pid = await _resolve_project_id(ProjectClient(http), proj)
        kc = KnowledgeClient(http, pid)
        eid = await kc.resolve_entity(identifier)
        ent = await kc.get_entity(eid)
        return ent.content or "(leer)"


async def _bm_wait_materialized() -> None:
    """Basic Memory schreibt die .md-Datei NICHT synchron zur create/update-Antwort
    (Server antwortet 202 Accepted, Materialisierung laeuft als In-Process-
    Hintergrund-Task im selben Event-Loop -- siehe note_content_materialization.py,
    LocalNoteContentMaterializationProvider.materialize_write_change). stage_runner.py
    ist ein kurzlebiger CLI-Prozess, der direkt danach beendet wird; ohne dieses join()
    stirbt der Prozess, bevor der Hintergrund-Worker die Datei geschrieben hat --
    create_entity/update_entity melden dann Erfolg, obwohl im Vault nie eine Datei
    entsteht (nur der SQLite-Index wird getroffen). Jan/der volle MCP-Stack laeuft
    dauerhaft und bekommt die Materialisierung dadurch "zufaellig" noch mit."""
    from basic_memory.index.note_content_materialization import _materialization_pool
    await _materialization_pool.join()


async def _bm_write(title: str, content: str, note_type: str, folder: str = "",
                     extra_metadata: Optional[dict] = None, project: Optional[str] = None):
    from basic_memory.mcp.async_client import get_client
    from basic_memory.mcp.clients.knowledge import KnowledgeClient
    from basic_memory.mcp.clients.project import ProjectClient
    proj = project or _bm_project()
    async with get_client(project_name=proj) as http:
        pid = await _resolve_project_id(ProjectClient(http), proj)
        kc = KnowledgeClient(http, pid)
        payload = {"title": title, "content": content, "directory": folder, "note_type": note_type}
        if extra_metadata:
            payload["entity_metadata"] = extra_metadata
        ent = await kc.create_entity(payload)
        await _bm_wait_materialized()
        return f"Gespeichert in '{proj}': {ent.permalink} [note_type={note_type}]"


async def _bm_update(identifier: str, content: str, note_type: Optional[str] = None,
                      project: Optional[str] = None):
    from basic_memory.mcp.async_client import get_client
    from basic_memory.mcp.clients.knowledge import KnowledgeClient
    from basic_memory.mcp.clients.project import ProjectClient
    proj = project or _bm_project()
    async with get_client(project_name=proj) as http:
        pid = await _resolve_project_id(ProjectClient(http), proj)
        kc = KnowledgeClient(http, pid)
        eid = await kc.resolve_entity(identifier)
        cur = await kc.get_entity(eid)
        directory = cur.file_path.rsplit("/", 1)[0] if "/" in cur.file_path else ""
        ent = await kc.update_entity(eid, {"title": cur.title, "content": content,
                                            "directory": directory,
                                            "note_type": note_type or cur.note_type})
        await _bm_wait_materialized()
        return f"Aktualisiert in '{proj}': {ent.permalink}"


def run_async(coro):
    import asyncio
    try:
        return asyncio.run(coro)
    except Exception as exc:
        return f"Fehler: {exc}"


# ---------------------------------------------------------------------------
# Tool: run_substage -- NUR in der "main"-Rolle verfuegbar (siehe run_stage).
# Eigener Subprozess (python stage_runner.py --role worker ...) statt
# rekursivem In-Process-Aufruf von run_stage(): das globale _BM_PROJECT wuerde
# sonst durch die Sub-Stage ueberschrieben und nach deren Ende nicht wieder-
# hergestellt. Rolle fest auf "worker", damit eine Sub-Stage nicht ihrerseits
# per run_substage weiter-delegieren kann (keine unbegrenzte Rekursionstiefe).
# Bekannte Einschraenkung (bewusst, kein Job-Object wie main.py/P4): ein
# Timeout killt nur den direkten Subprozess, keine von ihm gestarteten
# pwsh-Kindprozesse -- in der Praxis greift vorher fast immer die interne
# WALL_CLOCK_LIMIT_S-Notbremse der Sub-Stage selbst (sauberer Exit).
# ---------------------------------------------------------------------------
_SUBSTAGE_FINISH_MARKER = "=== STAGE BEENDET"
_SUBSTAGE_TS_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ", flags=re.M)


def _extract_substage_summary(rc: int, stderr_text: str) -> str:
    idx = stderr_text.find(_SUBSTAGE_FINISH_MARKER)
    if idx != -1:
        block = stderr_text[idx + len(_SUBSTAGE_FINISH_MARKER):]
        nl = block.find("\n")
        if nl != -1:
            block = block[nl + 1:]
        text = _SUBSTAGE_TS_PREFIX.sub("", block).strip()
        if text:
            return text
    fallback = {
        0: "(finish_stage-Zusammenfassung nicht im Log gefunden)",
        1: "Sub-Stage-Fehler oder success=false.",
        2: "Sub-Stage abgebrochen: Wall-Clock-Limit erreicht.",
        3: "Sub-Stage abgebrochen: maximale Iterationszahl erreicht.",
    }
    return fallback.get(rc, f"Unbekannter Exit-Code {rc}.")


def tool_run_substage(task: str, model: str = "qwen3:8b", project: Optional[str] = None,
                       max_iterations: int = MAX_ITERATIONS, temperature: float = 0.6,
                       think: bool = False) -> str:
    proj = project or _bm_project()
    # tool_run_substage blockiert (subprocess.run) -- ohne diesen Marker loggt
    # stage_runner.py waehrend der ganzen Laufzeit der Sub-Stage NICHTS, die GUI
    # (Panel "Hintergrundaufgaben") haette also keine Moeglichkeit, "laeuft
    # gerade" von "noch nicht gestartet" zu unterscheiden -- der normale
    # TOOL-run_substage(...)-Log kommt erst NACH Abschluss (dispatch-Schleife).
    preview = task.replace("\n", " ").strip()[:80]
    _log(f"run_substage GESTARTET: {preview}")
    fd, task_path = tempfile.mkstemp(prefix="substage-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(task)
        cmd = [sys.executable, str(Path(__file__).resolve()),
               "--model", model, "--project", proj, "--task", f"@{task_path}",
               "--max-iterations", str(max_iterations), "--temperature", str(temperature),
               "--think", "on" if think else "off", "--role", "worker"]
        try:
            # PYTHONIOENCODING=utf-8: ohne das schreibt die Sub-Stage ohne Konsole
            # unter Windows in der System-Codepage (hier 850), nicht UTF-8 -- die
            # explizite encoding="utf-8" hier dekodiert sonst falsche Bytes.
            sub_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=WALL_CLOCK_LIMIT_S + 60,
                                   creationflags=CREATE_NO_WINDOW, env=sub_env)
            rc = proc.returncode
            stderr_text = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            rc = 2
            stderr_text = (exc.stderr or "") + "\n[Backstop-Timeout in run_substage ausgeloest]"
    finally:
        try:
            os.unlink(task_path)
        except OSError:
            pass

    outcome = {0: "erfolgreich", 1: "mit success=false/Fehler beendet",
               2: "Wall-Clock-Notbremse", 3: "max_iterations ohne finish_stage"}.get(rc, f"rc={rc}")
    summary = _extract_substage_summary(rc, stderr_text)
    return f"Substage {outcome} (Modell={model}, Projekt={proj}).\n\n{summary}"


# ---------------------------------------------------------------------------
# Tool-Schemas (OpenAI-Format, von Ollama akzeptiert)
# ---------------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {
        "name": "run_powershell",
        "description": "Fuehrt einen PowerShell-Befehl auf dem lokalen Host aus (echter Host-Zugriff, kein Sandbox-Tool).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "workdir": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Websuche (lokale SearXNG-Instanz). Kurze Snippets. Bevorzugt gegenueber web_fetch_page.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "max_results": {"type": "integer"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "web_fetch_page",
        "description": "Laedt eine Seite, Klartext, hart gekuerzt auf max_chars (Default 2500, Obergrenze 8000).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "max_chars": {"type": "integer"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "memory_list_projects",
        "description": "Listet alle verfuegbaren Basic-Memory-Projekte (z. B. security-v2, it-technik-v2, trading-v2, 18bravo-v2, coding-v2). Nutzen, bevor man project in einem anderen Memory-Tool angibt, falls der genaue Name unklar ist.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "Durchsucht ein Basic-Memory-Projekt nach Fakten/Entscheidungen/Notizen. Ohne project: das Projekt dieser Stage.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "entity_type": {"type": "string", "enum": ["", "entity", "observation", "relation"]},
            "category": {"type": "string", "enum": ["", "open", "decision", "fact", "constraint", "risk"]},
            "limit": {"type": "integer"},
            "project": {"type": "string", "description": "Anderes Projekt als das der Stage durchsuchen, z. B. 'security-v2'. Siehe memory_list_projects."},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "memory_read",
        "description": "Liest eine Notiz vollstaendig anhand ihres Permalinks/Identifiers. Ohne project: das Projekt dieser Stage.",
        "parameters": {"type": "object", "properties": {
            "identifier": {"type": "string"},
            "project": {"type": "string", "description": "Anderes Projekt als das der Stage. Siehe memory_list_projects."},
        }, "required": ["identifier"]},
    }},
    {"type": "function", "function": {
        "name": "memory_write",
        "description": (
            "Legt eine neue Notiz an. note_type PFLICHT: system_state|reference|decision_record|incident|history|guide. "
            "Ohne project: das Projekt dieser Stage -- in ein ANDERES Projekt schreiben nur mit klarem Grund "
            "(z. B. ein Sicherheitsbefund gehoert nach security-v2, nicht ins eigene Projekt)."
        ),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "content": {"type": "string"},
            "note_type": {"type": "string"}, "folder": {"type": "string"},
            "project": {"type": "string", "description": "Anderes Projekt als das der Stage. Siehe memory_list_projects."},
        }, "required": ["title", "content", "note_type"]},
    }},
    {"type": "function", "function": {
        "name": "memory_update",
        "description": "Ersetzt den gesamten Inhalt einer vorhandenen Notiz (kein Anhaengen). Ohne project: das Projekt dieser Stage.",
        "parameters": {"type": "object", "properties": {
            "identifier": {"type": "string"}, "content": {"type": "string"}, "note_type": {"type": "string"},
            "project": {"type": "string", "description": "Anderes Projekt als das der Stage. Siehe memory_list_projects."},
        }, "required": ["identifier", "content"]},
    }},
    {"type": "function", "function": {
        "name": "finish_stage",
        "description": (
            "Beendet diese Stage. PFLICHT-Aufruf, sobald das Stage-Ziel erreicht ist -- niemals einfach "
            "aufhoeren zu antworten. Dauerhaftes Wissen vorher per memory_write/memory_update sichern; "
            "hier nur eine kurze Zusammenfassung fuer die Konsole."
        ),
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "Kurze Zusammenfassung: was wurde erreicht, was ist offen."},
            "success": {"type": "boolean"},
        }, "required": ["summary", "success"]},
    }},
]

TOOL_IMPL = {
    "run_powershell": lambda a: tool_run_powershell(**a),
    "web_search": lambda a: tool_web_search(**a),
    "web_fetch_page": lambda a: tool_web_fetch_page(**a),
    "memory_list_projects": lambda a: run_async(_bm_list_projects()),
    "memory_search": lambda a: run_async(_bm_search(
        a.get("query", ""), a.get("entity_type", ""), a.get("category", ""), a.get("limit", 10),
        a.get("project"))),
    "memory_read": lambda a: run_async(_bm_read(a["identifier"], a.get("project"))),
    "memory_write": lambda a: run_async(_bm_write(
        a["title"], a["content"], a["note_type"], a.get("folder", ""), project=a.get("project"))),
    "memory_update": lambda a: run_async(_bm_update(
        a["identifier"], a["content"], a.get("note_type"), a.get("project"))),
}

# NUR fuer die "main"-Rolle: run_substage delegiert an eine Worker-Stage (eigener
# Subprozess, Rolle dort fest "worker" -- siehe tool_run_substage). Worker-Stages
# selbst bekommen dieses Tool NICHT (TOOLS/TOOL_IMPL oben bleiben ihr Werkzeugsatz).
_RUN_SUBSTAGE_SCHEMA = {"type": "function", "function": {
    "name": "run_substage",
    "description": (
        "Delegiert eine abgegrenzte Teilaufgabe an eine Worker-Stage mit FRISCHEM Kontext "
        "(eigener Ollama-Request-Zyklus, Default-Modell qwen3:8b, kein Reasoning). Du siehst "
        "nur die finish_stage-Zusammenfassung, nicht den Tool-Verlauf -- hilft, den eigenen "
        "Kontext fuer rechenintensive/rechercheintensive Nebenaufgaben klein zu halten. NICHT "
        "fuer die eigentliche Code-Arbeit oder triviale Ein-Schritt-Abfragen."
    ),
    "parameters": {"type": "object", "properties": {
        "task": {"type": "string", "description": "Vollstaendige, in sich geschlossene Aufgabenbeschreibung -- die Sub-Stage sieht NICHTS aus diesem Chat."},
        "model": {"type": "string"}, "project": {"type": "string"},
        "max_iterations": {"type": "integer"}, "temperature": {"type": "number"},
        "think": {"type": "boolean"},
    }, "required": ["task"]},
}}

TOOLS_MAIN = TOOLS + [_RUN_SUBSTAGE_SCHEMA]
TOOL_IMPL_MAIN = {**TOOL_IMPL, "run_substage": lambda a: tool_run_substage(**a)}


SYSTEM_PROMPT_TEMPLATE = """\
Du bist eine einzelne, fokussierte Arbeits-Stage eines mehrstufigen Agenten-Systems. \
Du laeufst OHNE menschliche Aufsicht bis zum Ende dieser Stage.

WICHTIGSTE REGEL: Sobald das unten genannte Stage-Ziel erreicht ist (oder du zweifelsfrei \
feststellst, dass es mit den verfuegbaren Mitteln nicht erreichbar ist), rufst du das Tool \
`finish_stage` auf. Nicht einfach aufhoeren zu antworten, nicht endlos weiterarbeiten. \
Dauerhafte Fakten/Entscheidungen/Ergebnisse sicherst du VORHER per memory_write/memory_update \
im Basic-Memory-Projekt '{project}' -- die naechste Stage hat KEINEN Zugriff auf diesen \
Gespraechsverlauf, nur auf das, was du ins Memory geschrieben hast.

MEMORY-FIRST: Bevor du einen Fakt (Flags, Versionen, Umgebungsdetails, fruehere Entscheidungen) \
per Shell/Web neu verifizierst, pruefe ZUERST memory_search/memory_read, ob dazu schon eine \
Notiz existiert. Re-Verifikation nur bei explizitem Zweifel oder wenn die Notiz das als offen \
markiert -- sonst nutze die Notiz direkt.

ANDERE MEMORY-PROJEKTE: Die Memory-Tools akzeptieren optional ein project-Argument fuer ein \
anderes Basic-Memory-Projekt als '{project}' (z. B. security-v2, it-technik-v2, trading-v2, \
18bravo-v2 -- memory_list_projects zeigt alle). Nur nutzen, wenn fuer DIESES Stage-Ziel klar \
sachlich zustaendig (z. B. ein Sicherheitsbefund gehoert nach security-v2). Standard ist immer \
'{project}'.

WEB-RECHERCHE: web_search zuerst (kurze Snippets), web_fetch_page nur wenn das Snippet nicht \
reicht, max_chars klein halten. Fuer exakte CLI-Flags/Optionen eines Tools: wenn das Tool lokal \
oder per SSH erreichbar ist, dessen eigenes --help/man nutzen statt Web-Doku -- massgeblich fuer \
die tatsaechlich installierte Version.

SHELL: run_powershell hat echten Host-Zugriff (kein Sandbox-Tool).

DEINE ROLLE -- AUSFUEHREN, NICHT ENTSCHEIDEN: Du fuehrst das Stage-Ziel exakt aus, extrahierst \
Fakten aus Tool-Output, verifizierst sie und meldest sie. Du bildest KEINE Hypothesen ueber \
Dinge, die du nicht direkt per Tool geprueft hast, und triffst KEINE Architektur- oder \
Design-Entscheidungen -- das bleibt der aufrufenden Instanz vorbehalten, die diese Stage \
gestartet hat. Wenn eine Information fehlt, unklar oder widerspruechlich ist: das explizit als \
offenen Punkt in finish_stage(summary=...) melden (success=false, falls das Stage-Ziel dadurch \
nicht erreichbar war) statt zu raten oder eine plausibel klingende Annahme als Fakt auszugeben.

STAGE-ZIEL:
{task}
"""


# "main"-Rolle: Reapers eigentliche Agenten-Identitaet (adaptiert aus Jans
# assistant.json -- gleiche Prinzipien, aber mit den tatsaechlichen Tool-Namen
# dieses Skripts statt Jans MCP-Namen mit _coding_v2-Suffix). Im Unterschied
# zur "worker"-Rolle DARF/SOLL dieser Modus Entscheidungen treffen -- das ist
# der Haupt-Chat-Agent, kein Wegwerf-Arbeitsschritt. Nur in dieser Rolle steht
# run_substage zur Verfuegung (Delegation an einen Worker), damit Worker-
# Stages nicht ihrerseits rekursiv weiter-delegieren.
MAIN_SYSTEM_PROMPT_TEMPLATE = """\
Du bist REAPER, ein autonomer Software-Engineering-Agent: Analyse, Implementierung, \
Debugging, Refactoring, Tests, technische Problemloesung. Du arbeitest wie ein erfahrener \
Senior-Engineer -- analytisch, pragmatisch, mit solidem technischen Verstaendnis.

Leitsatz: Erst verstehen. Praezise aendern. Alles verifizieren.

GRUNDSAETZE
- Evidenz statt Annahme: erfinde niemals Dateien, Funktionssignaturen, Testergebnisse. \
Inspiziere den tatsaechlichen Code/Zustand per Tool, wo immer moeglich. Trenne klar zwischen \
verifizierten Fakten, Hypothesen und unverifizierten Annahmen.
- Minimale Intervention: die kleinste technisch korrekte Aenderung, die das Problem vollstaendig \
loest. Kein unbeteiligtes Refactoring, keine Formatierungsaenderungen, keine Architekturaenderung \
ohne Anlass. Bestehendes Verhalten erhalten, falls nicht explizit anders verlangt.
- Ursache statt Symptom beim Debuggen: Fehler beobachten -> betroffene Komponenten eingrenzen -> \
Ausfuehrungspfad verfolgen -> reproduzieren wenn machbar -> Ursache feststellen -> gezielt beheben \
-> verifizieren.

AUTONOMIE -- du darfst eigenstaendig entscheiden und handeln (das unterscheidet dich von einer \
delegierten Worker-Stage): Code inspizieren/aendern, Diagnosen fahren, Tests/Builds ausfuehren, \
Regressionstests schreiben, eigene Fehler korrigieren. Bestaetigung einholen vor: destruktiven/\
irreversiblen Aktionen, Force-Push/History-Ueberschreiben, Releases/Produktion, grossen \
Architekturaenderungen, System-Software-Installation, Aenderungen an Secrets/Produktionsinfrastruktur.

KOMMUNIKATION: deutsch, praezise, ohne Marketing-Floskeln oder unnoetige Einleitungen. Bei \
groesseren Aufgaben: Befund, Loesung, Verifikation, verbleibende Risiken.

MEMORY-FIRST: Bevor du einen Fakt (Flags, Versionen, Umgebungsdetails, fruehere Entscheidungen) \
per Shell/Web neu verifizierst, pruefe ZUERST memory_search/memory_read im Basic-Memory-Projekt \
'{project}', ob dazu schon eine Notiz existiert. Re-Verifikation nur bei explizitem Zweifel oder \
wenn die Notiz das als offen markiert. memory_write/memory_update: note_type PFLICHT, dauerhaftes \
Wissen gehoert dorthin -- nicht in diesen Chatverlauf (der naechste Prozess sieht ihn nicht).

ANDERE MEMORY-PROJEKTE: memory_list_projects zeigt alle (z. B. security-v2, it-technik-v2, \
trading-v2, 18bravo-v2). Nur gezielt nutzen, wenn fuer die Sache klar zustaendig.

WEB-RECHERCHE: web_search zuerst (kurze Snippets), web_fetch_page nur wenn das nicht reicht, \
max_chars klein halten. Fuer exakte CLI-Flags/Optionen: wenn lokal erreichbar, dessen eigenes \
--help/man nutzen statt Web-Doku.

SHELL: run_powershell hat echten Host-Zugriff (kein Sandbox-Tool).

SUB-AGENT-DELEGATION (run_substage): startet eine Worker-Stage mit FRISCHEM Kontext gegen ein \
Ollama-Modell (du siehst nur deren finish_stage-Zusammenfassung, nicht den Tool-Verlauf). Nutzen \
fuer abgegrenzte, selbststaendig verifizierbare Teilaufgaben, die sonst viel von deinem eigenen \
Kontext fressen wuerden (z. B. mehrere CLI-Flags per --help verifizieren, fokussierte Recherche). \
NICHT nutzen fuer: die eigentliche Code-Arbeit selbst, alles was deinen eigenen Chatverlauf \
braucht, triviale Ein-Schritt-Abfragen. Nach Rueckgabe: wie bei Memory-Content, der Zusammenfassung \
vertrauen aber konkrete Behauptungen vor folgenreichen Schritten selbst verifizieren.

Du rufst `finish_stage` NICHT selbst auf einem vorzeitigen Punkt auf -- in dieser Rolle laeufst du \
im normalen Chat-Dialog; `finish_stage` beendet NUR diese eine Antwort-Runde (Pflicht, sobald dein \
Beitrag zu dieser Nachricht fertig ist), nicht die Unterhaltung insgesamt.

AKTUELLER AUFTRAG (ggf. mit kurzem Verlauf vorheriger Turns dieser Session):
{task}
"""


def call_ollama(model: str, messages: list[dict], temperature: float = 0.6,
                 think: Optional[bool] = None, tools: list[dict] = TOOLS,
                 num_ctx: int = DEFAULT_NUM_CTX) -> dict:
    options: dict[str, Any] = {"temperature": temperature}
    # B07: pass num_ctx explicitly so the context window is deterministic and does
    # not silently depend on the model's Modelfile default. We do NOT edit the
    # global Modelfile; this is per-request only.
    if num_ctx:
        options["num_ctx"] = int(num_ctx)
    body: dict[str, Any] = {
        "model": model, "messages": messages, "tools": tools,
        "stream": False, "options": options,
    }
    if think is not None:
        body["think"] = think
    resp = requests.post(OLLAMA_URL, json=body, timeout=600)
    # B07: on an error status, surface Ollama's actual response body instead of
    # discarding it (raise_for_status loses it) -- essential to tell a confirmed
    # context error apart from a generic HTTP 500.
    if resp.status_code >= 400:
        try:
            body_text = resp.text or ""
        except Exception:
            body_text = "(response body unavailable)"
        raise OllamaHTTPError(resp.status_code, body_text)
    return resp.json()


def run_stage(model: str, project: str, task: str, max_iterations: int,
              temperature: float, think: Optional[bool] = None, role: str = "worker",
              num_ctx: int = DEFAULT_NUM_CTX) -> dict:
    """Fuehrt eine Stage bis zum finish_stage-Aufruf (oder einem Abbruchgrund) aus.

    Rueckgabe: {"rc": int, "summary": str, "iterations": int}. rc: 0=success,
    1=Fehler/success=false, 2=Wall-Clock-Notbremse, 3=max_iterations ohne finish_stage.
    think: None laesst das Modell seinen eigenen Default nutzen (bei Qwen3 i.d.R. an);
    False erzwingt kein Reasoning (nur ueber Ollamas natives /api/chat moeglich, siehe
    OLLAMA_URL) -- passend zur Worker-Rolle ("ausfuehren, nicht entscheiden").
    role: "worker" (Default, unveraendertes Verhalten -- fuer run_substage/Jan-MCP-
    Delegation) oder "main" (Reapers eigentliche Agenten-Identitaet: eigener System-
    Prompt mit Entscheidungsbefugnis, zusaetzliches run_substage-Tool). Nur "main"
    bekommt run_substage, damit Worker-Stages nicht rekursiv weiter-delegieren.
    """
    global _BM_PROJECT
    _BM_PROJECT = project

    if role == "main":
        system = MAIN_SYSTEM_PROMPT_TEMPLATE.format(project=project, task=task)
        tools, tool_impl = TOOLS_MAIN, TOOL_IMPL_MAIN
    else:
        system = SYSTEM_PROMPT_TEMPLATE.format(project=project, task=task)
        tools, tool_impl = TOOLS, TOOL_IMPL
    messages: list[dict] = [{"role": "system", "content": system},
                             {"role": "user", "content": task}]

    _log(f"Stage gestartet -- Modell={model} Projekt={project} think={think} role={role}")
    _log(f"context config: num_ctx={num_ctx} budget_tokens={_context_budget(num_ctx, tools)} "
         f"shell_output_limit={SHELL_OUTPUT_LIMIT}")
    # R08: which PowerShell run_powershell uses. Only a non-packaged (regular
    # Win32) PowerShell 7 is accepted, because it is contained by the API's job
    # object (abort/watchdog); if none was found, run_powershell refuses to launch
    # a shell. A startup line so the operator can see the resolved path.
    if PWSH:
        _log(f"powershell: {PWSH} (non-packaged, job-contained)")
    else:
        _log("WARNING: no non-packaged PowerShell 7 found -- run_powershell will "
             "refuse to execute. Install it: winget install --id Microsoft.PowerShell "
             "--source winget (or set REAPER_PWSH to a non-packaged pwsh 7). "
             "See docs/operations.md.")
    start = time.monotonic()
    stage_tag = f"{project}-{role}"
    context_retry_done = False  # B07: at most one controlled context-reduction retry

    for i in range(1, max_iterations + 1):
        if time.monotonic() - start > WALL_CLOCK_LIMIT_S:
            _log(f"NOTBREMSE: Wall-Clock-Limit ({WALL_CLOCK_LIMIT_S}s) erreicht, breche Stage ab.")
            return {"rc": 2, "summary": f"Abgebrochen: Wall-Clock-Limit ({WALL_CLOCK_LIMIT_S}s) erreicht, kein finish_stage-Aufruf.", "iterations": i}

        # B07: bound the conversation history BEFORE the request so it can never
        # grow past the context budget (oldest tool outputs are compacted first;
        # system prompt, task and recent messages are preserved).
        compacted, est_tokens = _compact_history(messages, num_ctx, tools)

        _log(f"Iteration {i}/{max_iterations}: sende {len(messages)} Nachrichten an {model} ...")
        try:
            resp = call_ollama(model, messages, temperature, think, tools, num_ctx)
        except OllamaHTTPError as exc:
            _log(f"FEHLER beim Ollama-Request: HTTP {exc.status_code} -- body: {exc.body_text[:800]}")
            if _is_context_error(exc) and not context_retry_done:
                # Confirmed context error: one controlled reduction + single retry.
                # No tool was (re-)executed in this branch, so the retry cannot
                # duplicate a tool action. Tighter tail protection frees more room.
                context_retry_done = True
                hard_compacted, est_after = _compact_history(messages, num_ctx, tools, protect_tail=2)
                _log(f"context error confirmed -> one controlled context reduction "
                     f"(context_compaction={hard_compacted} est_prompt_tokens={est_after}), retrying once")
                try:
                    resp = call_ollama(model, messages, temperature, think, tools, num_ctx)
                except Exception as exc2:
                    _log(f"retry after context reduction failed: {exc2}")
                    return {"rc": 1,
                            "summary": f"Ollama context error; retry after reduction failed: {exc2}",
                            "iterations": i}
            else:
                reason = ("context exhaustion, retry already used" if _is_context_error(exc)
                          else "generic HTTP error (no context-specific retry)")
                return {"rc": 1,
                        "summary": f"Ollama HTTP {exc.status_code} ({reason}): {exc.body_text[:300]}",
                        "iterations": i}
        except Exception as exc:
            _log(f"FEHLER beim Ollama-Request: {exc}")
            return {"rc": 1, "summary": f"Fehler beim Ollama-Request: {exc}", "iterations": i}

        # B07 per-iteration diagnostics (compact; never full prompts/outputs).
        _log(f"diag iteration={i} num_ctx={num_ctx} "
             f"prompt_tokens={resp.get('prompt_eval_count')} "
             f"response_tokens={resp.get('eval_count')} "
             f"history_messages={len(messages)} est_prompt_tokens={est_tokens} "
             f"context_compaction={compacted}")

        msg = resp.get("message", {})
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        if content.strip():
            preview = content.strip().replace("\n", " ")[:200]
            _log(f"  Text: {preview}")

        # Ollamas natives API gibt tool_calls OHNE "id" zurueck und "arguments" bereits
        # als Objekt (nicht als JSON-String wie beim OpenAI-kompatiblen Endpoint) --
        # das Message-Objekt 1:1 unveraendert in die Historie uebernehmen.
        messages.append(msg)

        if not tool_calls:
            if i == max_iterations:
                break
            _log("  (keine Tool-Calls, kein finish_stage -- erinnere das Modell)")
            messages.append({"role": "user",
                              "content": "Bitte ruf finish_stage auf, sobald das Stage-Ziel "
                                         "erreicht ist -- oder mach mit einem Tool-Call weiter."})
            continue

        for idx, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}

            if name == "finish_stage":
                summary = args.get("summary", "(keine Zusammenfassung)")
                success = args.get("success", True)
                _log(f"=== STAGE BEENDET (success={success}) ===")
                _log(summary)
                return {"rc": 0 if success else 1, "summary": summary, "iterations": i}

            impl = tool_impl.get(name)
            if impl is None:
                result = f"Unbekanntes Tool: {name}"
            else:
                try:
                    result = impl(args)
                except Exception as exc:
                    result = f"Fehler beim Ausfuehren von {name}: {exc}"

            full_result = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            _diag_dump(stage_tag, f"iter{i}_{name}", full_result)  # opt-in full output
            result_str, tr = _cap_output(full_result, SHELL_OUTPUT_LIMIT)
            _log(f"  TOOL {name}({json.dumps(args, ensure_ascii=False)[:150]}) -> "
                 f"{len(result_str)} Zeichen{' (truncated)' if tr else ''}")

            messages.append({
                "role": "tool", "tool_name": name, "content": result_str,
            })

    _log("Maximale Iterationszahl erreicht, ohne finish_stage-Aufruf -- Stage NICHT sauber beendet.")
    return {"rc": 3, "summary": "Abgebrochen: maximale Iterationszahl erreicht, kein finish_stage-Aufruf.", "iterations": max_iterations}


def main() -> int:
    global SHELL_OUTPUT_LIMIT  # B07: --shell-output-limit may override the module value
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Ollama-Modellname, z.B. qwen36-a3b:latest")
    ap.add_argument("--project", required=True, help="Basic-Memory-Projektname, z.B. coding-v2")
    ap.add_argument("--task", required=True, help="Stage-Auftrag (Text oder @datei.txt)")
    ap.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--think", choices=["on", "off", "auto"], default="auto",
                     help="auto=Modell-Default (i.d.R. an), on=erzwungen an, off=erzwungen aus "
                          "(nur ueber Ollamas natives /api/chat wirksam)")
    ap.add_argument("--role", choices=["worker", "main"], default="worker",
                     help="worker (Default) = Ausfuehrender Arbeitsschritt ohne Entscheidungs-"
                          "befugnis (Jan-run_substage-Verhalten, unveraendert). main = Reapers "
                          "eigentliche Agenten-Identitaet mit Entscheidungsbefugnis + run_substage.")
    # B07: context window + tool-output limit configurable (also via REAPER_NUM_CTX /
    # REAPER_SHELL_OUTPUT_LIMIT). Defaults mirror the env-backed module constants.
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                     help="Ollama context window (num_ctx), passed explicitly per request. "
                          "Default 65536 (or REAPER_NUM_CTX). Never raised automatically.")
    ap.add_argument("--shell-output-limit", type=int, default=SHELL_OUTPUT_LIMIT,
                     help="Max characters kept per tool result (head+tail, marked). "
                          "Default 8000 (or REAPER_SHELL_OUTPUT_LIMIT).")
    args = ap.parse_args()

    # Apply the configured per-tool-result cap globally (read by tool_run_powershell
    # and run_stage).
    SHELL_OUTPUT_LIMIT = args.shell_output_limit

    task = args.task
    if task.startswith("@"):
        task = open(task[1:], encoding="utf-8").read()

    think = {"on": True, "off": False, "auto": None}[args.think]
    result = run_stage(args.model, args.project, task, args.max_iterations, args.temperature,
                        think, args.role, num_ctx=args.num_ctx)
    # os._exit() statt sys.exit(): aiosqlite startet seinen Connection-Worker-Thread
    # OHNE daemon=True (aiosqlite/core.py) -- bleibt so ein Thread nach einer
    # memory_*-Operation haengen (beobachtet nach Stages mit vielen Memory-Zugriffen),
    # wartet der normale Python-Shutdown unbegrenzt auf diesen Non-Daemon-Thread und
    # der Prozess haengt, obwohl run_stage() laengst fertig ist (finish_stage bereits
    # geloggt, alle Writes bereits materialisiert). os._exit() beendet den Prozess
    # sofort auf OS-Ebene, unabhaengig vom Thread-Zustand -- sicher, weil an dieser
    # Stelle keine Arbeit mehr aussteht.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(result["rc"])


if __name__ == "__main__":
    main()
