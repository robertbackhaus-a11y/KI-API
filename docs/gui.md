<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Chat GUI

Status: version 0.2.0. Source: `src/reaper_ai/web/index.html` (a single file,
vanilla JavaScript, no build step, no framework). The GUI only talks to the
endpoints from [api.md](api.md) and knows nothing about `stage_runner.py`, Ollama
or Basic Memory. GUI texts are in English; messages and answers are shown in the
language they were written in.

## Controls

- **Header:** session id, status light from `GET /health` (API / Ollama / Runner,
  every 5 s), model selection (`qwen3.8-q5:latest` main, `qwen3:8b` fast),
  Basic Memory project, Reasoning switch (`think`), "New session", "Panel".
- **History:** user messages and stage answers with status badge and duration;
  the stderr tail can be expanded under "Details". Enter sends, Shift+Enter
  inserts a line break; while a stage runs, "Abort" appears.
- **Side panel** (open/closed state is saved):
  - "Activity": `log_line` events live.
  - "Background tasks": detects started (`run_substage GESTARTET:`) and finished
    (`TOOL run_substage(`) sub-stages by pattern matching on the log. Purely
    cosmetic; the API does not interpret the log text. The markers are written by
    `stage_runner.py` and are matched verbatim.
  - "Context": preview from `GET /sessions/{id}/context`.

## Markdown and syntax highlighting

- Answers are rendered with marked (GFM, single line breaks are preserved).
- highlight.js colors code blocks: language from the fence, otherwise
  auto-detection over a fixed list (python, powershell, bash, json, yaml,
  javascript, typescript, xml, sql, csharp, java, go, rust, cpp, diff) with a
  minimum relevance. Every code block gets a header with the language name and
  "Copy".
- Security: raw HTML from the model answer is shown as text; external images are
  not loaded but shown as a link; the result goes through DOMPurify (among other
  things without `style`, `img`, form elements, `id`/`name` attributes, `data-*`;
  only classes the rendering itself produces). Links open in a new tab with
  `rel="noopener noreferrer"`.
- Rendered HTML is cached per answer text (LRU, 300 entries); unchanged answers
  are not set again.
- If the libraries are missing, the GUI shows plain text.
- The highlight theme (light/dark) follows `prefers-color-scheme`.

## Copying

"Copy answer" copies the Markdown source of the answer, "Copy" on a code block
copies its content. The button briefly shows "Copied" (or "Copy failed"). Without
a secure context (Clipboard API not available) a fallback via
`execCommand('copy')` is used.

## Recovery after a lost connection (SSE recovery)

If the SSE stream is lost (reload, network drop, server restart), `POST /messages`
returns 409, or the stream ends without a terminal event, the GUI polls
`GET /sessions/{id}` every 2 s until no stage is `running` anymore and shows the
final result (including `aborted`, `watchdog_timeout`, `incomplete`).

- `POST /messages` is never sent again. Live logs from the meantime are lost.
- The history is only rebuilt when the message count or stage status changed;
  the rebuild itself is complete (the server is the only truth, no duplicate
  entries).
- If the server still does not know a sent message after two polls, the GUI
  reports it as not processed and puts the text back into the input field.
- 404 ends the recovery with a notice; if the server is unreachable, it keeps
  retrying.

## localStorage

| Key | Content |
|---|---|
| `reaper_session_id` | current session; a reload loads it, unknown → new session |
| `reaper_model`, `reaper_project`, `reaper_think` | last selection in the header |
| `reaper_panel_open` | side panel open/closed |

All accesses are guarded; the GUI keeps working without localStorage.

## Bundled libraries

Under `src/reaper_ai/web/vendor/`, served via `/vendor`, license texts alongside.
No CDN dependency, no npm.

| File | Library | License |
|---|---|---|
| `marked.umd.js` | marked 15.0.12 | MIT |
| `purify.min.js` | DOMPurify 3.4.16 | Apache-2.0 / MPL-2.0 |
| `highlight.min.js`, `hljs-powershell.min.js`, `hljs-github.min.css`, `hljs-github-dark.min.css` | highlight.js 11.12.0 | BSD-3-Clause |

Updating: take the files from the official release of the respective library,
keep the file names, adjust the versions in this table.
