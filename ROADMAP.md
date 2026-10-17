<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Reaper-AI – Development Roadmap

**Repository:** `C:\tools\src\Reaper-AI`  
**Platform:** Windows 11 / Python / FastAPI / Ollama  
**Installation:** `pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git` (binding standard, see rule 11)  
**Architecture:** Local single-user system

## Release v0.2.0 (released 2026-10-17)

Version `0.2.0` (single version source `src/reaper_ai/__init__.py`), annotated tag
`v0.2.0` on `master`, GitHub Release with wheel and sdist attached (no PyPI).
Install with `pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git@v0.2.0`.
Highlights since v0.1.0:

- **Integrated stage runner:** the runner ships inside the package as
  `reaper_ai.runner.stage_runner` (dependencies `requests`, `basic-memory`;
  Python ≥ 3.12), so a GitHub pip install is complete without a separate checkout.

- **R08 – PowerShell process containment:** stages now use a regular, non-packaged
  PowerShell 7 so abort/watchdog reliably kill all pwsh child/grandchild processes
  via the Windows job object. The packaged Store/MSIX PowerShell is never used and
  there is no fallback to Windows PowerShell 5.1; `run_powershell` refuses to start
  an uncontained shell and `reaper-ai run` warns if no suitable PowerShell 7 is
  installed. `run_powershell`'s default working directory moved to
  `<data-dir>/workspace` so relative model paths no longer pollute the data
  directory.

## Mandatory Rules

1. Keep the existing FastAPI architecture.
2. The stage runner (`reaper_ai.runner.stage_runner`, formerly the standalone `stage_runner.py`) remains the sole AI orchestrator and always runs as a separate subprocess.
3. No additional services, databases, containers or frontend build systems.
4. Install new Python dependencies only via `pip` and document them in `requirements.txt` with pinned versions.
5. Bundle JavaScript/CSS libraries as local, versioned files when needed. No CDN dependencies at runtime, no npm.
6. Preserve existing functionality and run regression tests after every phase.
7. Keep test scripts local and excluded from Git.
8. Implement changes separately per work package. Do not commit, push or deploy automatically.
9. Observe copyright and SPDX headers in new source files.
10. No unnecessary refactoring.
11. **Binding installation method:** users install Reaper-AI only with `pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git`, releases via their Git tag (`…Reaper-AI.git@v0.2.0`), and start it with `reaper-ai run`. No PyPI, no manual checkout, no `pip install -e .` for users (editable installs are for development only). The GitHub installation must stay complete: every package file (stage runner, GUI, `web/vendor/`, license texts) is tracked in Git and declared as package data, and every release is tested with `pip install git+…@<tag>` in a fresh venv.

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
    Exceptions: user prompts, AI-generated responses and stored conversation content keep their original language; Reaper continues to communicate with users in their preferred language. Protocol text emitted by the stage runner (e.g. the `=== STAGE BEENDET` summary marker and the `run_substage GESTARTET:` log marker) is matched verbatim and therefore stays as the runner writes it; the runner's German system prompts are model behaviour and are only translated in a separate, tested work package.

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
| B07 | Context window management in the stage runner: explicit `num_ctx` (default 65536, `REAPER_NUM_CTX`), deterministic history budget, tool output limit 8000 (`REAPER_SHELL_OUTPUT_LIMIT`), Ollama error body logging/classification with one controlled retry on confirmed context errors, per-iteration diagnostics | P1 | done (implemented in the standalone runner, carried over unchanged into the integrated runner, phase R; 28 local B07 tests green against the integrated runner) |

**Acceptance:** All previous 73 regression tests and newly added tests pass. No zombie processes, deadlocks or uncontrolled parallel GPU runs.

## Phase P – Packaging as a pip Package

Separate work package before the further GUI extensions (phase 1 onwards). Goal: installation and start like Chainlit, directly from GitHub (P11).

```
pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git
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
| P05 | Stage runner remains an external subprocess, `stage_runner.py` unchanged | done (superseded by phase R: runner integrated, still a separate subprocess) |
| P06 | Runner path and Ollama endpoint configurable (CLI > env > `config.toml` > default) | done (runner path reduced to the external override `runner_script`/`runner_python` in phase R) |
| P07 | Local installation `pip install -e .` tested in a fresh venv | done |
| P08 | Wheel built and installation tested in a fresh venv | done |
| P09 | Take over existing sessions: `reaper-ai migrate <checkout>` (copy only); checkout operation (`run.ps1`) keeps using `sessions/` in the repo | done |
| P10 | PyPI publication prepared (`python -m build`, `twine check`) | dropped (superseded by P11: no PyPI) |
| P11 | Official installation directly from GitHub: `pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git`, releases via Git tag (`@v0.1.0`), start via `reaper-ai run`; documented as binding standard (rule 11, `docs/operations.md`). Verified: wheel/sdist contain runner, GUI, `web/vendor/` and licenses (no `MANIFEST.in` needed); fresh venvs installed from GitHub `master` and `@v0.1.0`; working tree incl. runner installed via `git+…@<tag>` from a local test clone (runner, CLI, GUI/vendor served, 22/22 integration checks with the installed interpreter) | done (the runner reaches GitHub with the next commit/tag) |

No new backend services, no additional frameworks, no functional changes to the stage runner. Note: since B07/phase R the runner also reads `REAPER_OLLAMA_URL`.

**Acceptance:** All regression tests green, `reaper-ai run/stop/--version` working from an editable, a wheel and a GitHub (`git+…`) installation, existing sessions preserved.

## Phase R – Stage Runner Integration

The former standalone runner `C:\tools\stage-runner\stage_runner.py` (B07 version) is part of the package as `src/reaper_ai/runner/stage_runner.py`.

| ID | Task | Status |
|---|---|---|
| R01 | Runner analysed (dependencies `requests`, `basic-memory` 0.23.2 with internal client API; no other local files needed; `mcp_server.py` (Jan MCP wrapper) not taken over) | done |
| R02 | Runner moved to `src/reaper_ai/runner/` (functionally unchanged; English module header, markers and German prompts kept) | done |
| R03 | API starts `<python> -P -m reaper_ai.runner.stage_runner` as a separate subprocess (same interpreter, `PYTHONPATH` = package parent, cwd = `<data-dir>/workspace`, see R08); job object, abort, watchdog, suspended start unchanged | done |
| R04 | `run_substage` worker subprocess runs the same integrated file | done |
| R05 | External runner only as an explicit override (`runner_script`/`runner_python`, tests and rollback); `runner_dir`/`--runner-dir` removed with a warning for old configs | done |
| R06 | `requests` and `basic-memory>=0.23.2,<0.24` in `pyproject.toml`/`requirements.txt`; `requires-python >= 3.12` | done |
| R07 | Tested: editable install, fresh wheel in a fresh venv, all regression suites, new integration suite (fake Ollama + real subprocesses/pwsh/Basic Memory), real Ollama stages | done |
| R08 | PowerShell process containment: the packaged (Store/MSIX) PowerShell 7 does not inherit the stage job object (verified at the process-tree level — both the WindowsApps alias and the direct package path break out; it is a property of the packaged build, not the alias), so its pwsh processes survived abort/watchdog. Fix: a regular, non-packaged PowerShell 7 is required; `config.resolve_powershell()` returns `REAPER_PWSH` (only if non-packaged) → regular PowerShell 7 (`C:\Program Files\PowerShell\7` or `%LOCALAPPDATA%\Programs\PowerShell\7`), with **no** fallback to the packaged build and **no** fallback to Windows PowerShell 5.1. If none is found, `run_powershell` returns a clear error and starts no shell (no uncontained process), and `reaper-ai run`/the API lifespan warn with the install command. Also fixed: `run_powershell`'s default working directory is now `<data-dir>/workspace` (`REAPER_WORKDIR`), not the data directory. Windows job objects, abort/watchdog, suspended start and B07 unchanged; nothing auto-installed. | done — verified end-to-end with the installed regular PowerShell 7.6.7 (`C:\Program Files\PowerShell\7\pwsh.exe`): integration suite 28/28 (A2/A3 kill real pwsh-7 child **and** grandchild + 4 R08 checks), all regression suites green; see `docs/architecture.md`, `docs/operations.md` |
| R09 | PowerShell 7 is a verified installation requirement: `reaper-ai run` checks for a non-packaged PowerShell 7 at startup and prints the `winget install --id Microsoft.PowerShell` command if missing; `pip install git+…`, `reaper-ai --version` and `--help` work without it (checked only at run start / before shell execution). No silent fallback to Store/MSIX or Windows PowerShell 5.1; no automatic installation. | done (see `docs/operations.md`; tested with and without a suitable PowerShell 7) |

**Acceptance:** functional parity with the standalone runner, no runner import in the API process, all regression tests green.

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
- Test installation in a fresh `.venv` exclusively via `pip install git+https://github.com/robertbackhaus-a11y/Reaper-AI.git@<tag>`.
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
