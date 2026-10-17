<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper-AI – Development Roadmap

**Repository:** `C:\tools\src\Reaper-AI`  
**Platform:** Windows 11 / Python / FastAPI / Ollama  
**Installation:** Python packages only via `pip` into `.venv`  
**Architecture:** Local single-user system

## Mandatory Rules

1. Keep the existing FastAPI architecture.
2. `stage_runner.py` remains the sole AI orchestrator.
3. No additional services, databases, containers or frontend build systems.
4. Install new Python dependencies only via `pip` and document them in `requirements.txt` with pinned versions.
5. Bundle JavaScript/CSS libraries as local, versioned files when needed. No CDN dependencies at runtime, no npm.
6. Preserve existing functionality and run regression tests after every phase.
7. Keep test scripts local and excluded from Git.
8. Implement changes separately per work package. Do not commit, push or deploy automatically.
9. Observe copyright and SPDX headers in new source files.
10. No unnecessary refactoring.

## Development Guidelines

Apply automatically to all future bugfixes, features, development phases, work packages and releases:

1. After every implementation, `README.md`, `ROADMAP.md` and the affected files under `docs/` must be reviewed and updated if necessary.
2. New features, API endpoints, configuration parameters, CLI commands and architecture changes must be documented.
3. Mark completed work packages as done in the roadmap.
4. Correct outdated or contradictory documentation.
5. The README stays the compact project overview; technical details belong in `docs/`.
6. Documentation must match the actual state of the implementation.
7. A work package is only complete when code, tests and documentation are consistent.
8. New and changed source files must contain the copyright/SPDX header.
9. Test files stay local and outside Git version control.
10. **Language policy:** English is the default language for the entire Reaper-AI project. This applies to:
    - source code, comments, docstrings and copyright headers
    - README, ROADMAP and all documentation
    - CLI commands, help messages and API responses
    - GUI labels, buttons, status messages and error messages
    - log messages and configuration descriptions
    - commit messages and release notes

    Existing German text is translated into English without changing functionality.
    Exceptions: user prompts, AI-generated responses and stored conversation content keep their original language; Reaper continues to communicate with users in their preferred language. Protocol text emitted by the unchanged `stage_runner.py` (e.g. the `=== STAGE BEENDET` summary marker and the `run_substage GESTARTET:` log marker) is matched verbatim and therefore stays as the runner writes it.

Mapping in `docs/`: `api.md` (endpoints, SSE, status), `gui.md` (chat GUI, libraries), `operations.md` (installation, CLI, configuration), `architecture.md` (components, flows, persistence, package layout).

## Phase 0 – Remaining Bugfixes

| ID | Task | Priority | Status |
|---|---|---|---|
| B01 | Global limit for parallel GPU-intensive main stages | P2 | open |
| B02 | Review and harden error handling when `TerminateJobObject()` fails | P2 | open |
| B03 | Review and fix thread synchronization of session state | P2 | open |
| B04 | Optimize SSE recovery polling and unnecessary redraws | P2 | partial: recovery polls every 2 s, the history is only rebuilt when the message count/stage status changed, Markdown rendering is cached; the rebuild itself is still complete |
| B05 | Harden JSONL recovery for incomplete events | P3 | open |
| B06 | Review memory and handle release in longer sessions | P3 | open |

**Acceptance:** All previous 73 regression tests and newly added tests pass. No zombie processes, deadlocks or uncontrolled parallel GPU runs.

## Phase P – Packaging as a pip Package

Separate work package before the further GUI extensions (phase 1 onwards). Goal: installation and start like Chainlit.

```
pip install reaper-ai
reaper-ai run
reaper-ai stop
reaper-ai --version
```

| ID | Task | Status |
|---|---|---|
| P01 | `pyproject.toml` and `src/` layout (`src/reaper_ai/`) | done |
| P02 | CLI entry point `reaper-ai` via `[project.scripts]` (`run`, `stop`, `migrate`, `--version`) | done |
| P03 | FastAPI app, web GUI and `web/vendor/` fully included in the package | done |
| P04 | Sessions, temp tasks, configuration and PID file outside the package (`%LOCALAPPDATA%\reaper-ai`, `--data-dir` / `REAPER_AI_HOME`) | done |
| P05 | Stage runner remains an external subprocess, `stage_runner.py` unchanged | done |
| P06 | Runner path and Ollama endpoint configurable (CLI > env > `config.toml` > default) | done |
| P07 | Local installation `pip install -e .` tested in a fresh venv | done |
| P08 | Wheel built and installation tested in a fresh venv | done |
| P09 | Take over existing sessions: `reaper-ai migrate <checkout>` (copy only); checkout operation (`run.ps1`) keeps using `sessions/` in the repo | done |
| P10 | PyPI publication prepared (`python -m build`, `twine check`), upload only after approval, TestPyPI first | prepared |

No new backend services, no additional frameworks, no functional changes to the stage runner. Note: `ollama_url` affects the health light and the startup check; `stage_runner.py` still uses its hard-coded URL.

**Acceptance:** All regression tests green, `reaper-ai run/stop/--version` working from an editable and from a wheel installation, existing sessions preserved.

## Phase L – English Language Policy

| ID | Task | Status |
|---|---|---|
| L01 | Language policy added to the development guidelines | done |
| L02 | Code comments, docstrings, log messages, API error texts and CLI output translated | done |
| L03 | GUI labels, buttons and status messages translated (`<html lang="en">`) | done |
| L04 | README, ROADMAP and `docs/` translated; `docs/betrieb.md` → `docs/operations.md`, `docs/architektur.md` → `docs/architecture.md` | done |

Runner protocol markers stay unchanged (see guideline 10). Stage summaries already stored in existing sessions are conversation content and are not rewritten.

## Phase 1 – Chat Basics

| ID | Feature | Status |
|---|---|---|
| F01 | Markdown rendering | done (marked 15.0.12, GFM, sanitized with DOMPurify 3.4.16) |
| F02 | Syntax highlighting for code | done (highlight.js 11.12.0 incl. PowerShell, light/dark theme) |
| F03 | Copy buttons for answers and code blocks | done |
| F04 | Session list and chat switching | open |
| F05 | Rename and delete sessions | open |
| F06 | Chat export as Markdown | open |

Protect Markdown output against unsafe HTML. Render only changed messages. Use local JavaScript libraries without npm. F01 to F03 fulfill this: the libraries live in `src/reaper_ai/web/vendor/` (details in `docs/gui.md`).

## Phase 2 – File Support

| ID | Feature |
|---|---|
| F07 | File upload via FastAPI |
| F08 | Per-session file storage |
| F09 | Image preview and file attachments in the chat |
| F10 | File size and type validation |

Install `python-multipart` via `pip`. No general file processing in the API; domain processing stays with the runner. Uploads live in the data directory (phase P), not in the package.

## Phase 3 – Agent Monitoring

| ID | Feature |
|---|---|
| F11 | Structured stage/sub-stage events |
| F12 | Tool calls with status and duration |
| F13 | Show technical status and domain result separately |
| F14 | Present error and abort causes traceably |

Structured events instead of log-text parsing. Extend the existing SSE, no new protocol or message broker.

## Phase 4 – Chat Workflows

| ID | Feature |
|---|---|
| F15 | Edit and re-run messages |
| F16 | Search across existing sessions |
| F17 | Reusable prompt templates |

When re-running, explicitly take into account that tool actions may be repeated.

## Phase 5 – Memory and Control

| ID | Feature |
|---|---|
| F18 | Show existing memory entries |
| F19 | Correct memory entries selectively |
| F20 | Human-in-the-loop for critical tool actions |

No second memory database. Human-in-the-loop requires a real pause of the runner before the tool executes.

## Phase 6 – Completion

- Full regression test.
- Integration test with a real Ollama and stage runner.
- Verify abort, watchdog, recovery and parallel sessions.
- Check all new files for copyright headers.
- Update `requirements.txt`, README and `docs/`.
- Test installation in a fresh `.venv` exclusively via `pip`.
- Produce a final technical review.

## Execution Instructions for Claude

Work through the roadmap in the given order, but always implement only **one work package per run**.

Check the actual code before every change. Do not re-develop functionality that is already implemented.

After every work package:

1. Name the changed files.
2. Document the test results.
3. Update the documentation according to the mandatory development guidelines.
4. Name remaining risks.
5. Propose the next work package.

No autonomous commits, pushes or deployments. No enterprise extensions.
