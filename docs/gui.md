<!--
Copyright (C) 2026 Robert Backhaus
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Chat-GUI

Stand: Version 0.6.0. Quelle: `src/reaper_ai/web/index.html` (eine Datei,
Vanilla-JavaScript, kein Build-Schritt, kein Framework). Die GUI spricht nur die
Endpunkte aus [api.md](api.md) an und kennt weder `stage_runner.py` noch Ollama
oder Basic-Memory.

## Bedienelemente

- **Kopfzeile:** Session-Id, Ampel aus `GET /health` (API / Ollama / Runner, alle
  5 s), Auswahl Modell (`qwen3.8-q5:latest` Haupt, `qwen3:8b` schnell),
  Basic-Memory-Projekt, Schalter Reasoning (`think`), "Neue Session", "Panel".
- **Verlauf:** Nutzer-Nachrichten und Stage-Antworten mit Status-Badge und Dauer;
  der stderr-Tail steht aufklappbar unter "Details". Enter sendet, Shift+Enter
  bricht um; während einer Stage erscheint "Abbrechen".
- **Seitenpanel** (offen/zu wird gespeichert):
  - "Aktivität": `log_line`-Events live.
  - "Hintergrundaufgaben": erkennt per Mustersuche im Log gestartete
    (`run_substage GESTARTET:`) und beendete (`TOOL run_substage(`) Sub-Stages. Rein
    kosmetisch; die API interpretiert den Logtext nicht.
  - "Kontext": Vorschau aus `GET /sessions/{id}/context`.

## Markdown und Syntax-Highlighting

- Antworten werden mit marked gerendert (GFM, einfache Zeilenumbrüche bleiben
  erhalten).
- Codeblöcke färbt highlight.js ein: Sprache aus dem Fence, sonst Auto-Erkennung
  über eine feste Liste (python, powershell, bash, json, yaml, javascript,
  typescript, xml, sql, csharp, java, go, rust, cpp, diff) mit Mindest-Relevanz.
  Jeder Codeblock bekommt einen Kopf mit Sprachname und "Kopieren".
- Sicherheit: rohes HTML aus der Modellantwort wird als Text angezeigt; externe
  Bilder werden nicht geladen, sondern als Link gezeigt; das Ergebnis läuft durch
  DOMPurify (u. a. ohne `style`, `img`, Formularelemente, `id`/`name`-Attribute,
  `data-*`; nur Klassen, die das Rendering selbst erzeugt). Links öffnen in einem
  neuen Tab mit `rel="noopener noreferrer"`.
- Gerendertes HTML wird pro Antworttext gecacht (LRU, 300 Einträge); unveränderte
  Antworten werden nicht neu gesetzt.
- Fehlen die Bibliotheken, zeigt die GUI reinen Text.
- Das Highlight-Theme (hell/dunkel) folgt `prefers-color-scheme`.

## Kopieren

"Antwort kopieren" kopiert den Markdown-Quelltext der Antwort, "Kopieren" am
Codeblock dessen Inhalt. Ohne sicheren Kontext (Clipboard-API nicht verfügbar)
greift ein Fallback über `execCommand('copy')`.

## Wiederaufnahme nach Verbindungsverlust (SSE-Recovery)

Geht der SSE-Stream verloren (Reload, Netzabbruch, Server-Neustart), liefert
`POST /messages` 409 oder wird ohne aktiven Stream abgebrochen, fragt
die GUI alle 2 s `GET /sessions/{id}` ab, bis keine Stage mehr `running` ist, und
zeigt das Endergebnis (auch `aborted`, `watchdog_timeout`, `incomplete`).

- Es wird nie erneut `POST /messages` gesendet. Live-Logs aus der Zwischenzeit sind
  verloren.
- Der Verlauf wird nur neu aufgebaut, wenn sich Nachrichtenzahl oder Stage-Status
  geändert haben; der Neuaufbau selbst ist vollständig (Server ist die einzige
  Wahrheit, keine doppelten Einträge).
- Kennt der Server eine abgeschickte Nachricht nach zwei Abfragen nicht, meldet die
  GUI sie als nicht verarbeitet und legt den Text zurück ins Eingabefeld.
- 404 beendet die Wiederaufnahme mit Hinweis; bei nicht erreichbarem Server wird
  weiter versucht.

## localStorage

| Schlüssel | Inhalt |
|---|---|
| `reaper_session_id` | aktuelle Session; Reload lädt sie, unbekannt → neue Session |
| `reaper_model`, `reaper_project`, `reaper_think` | letzte Auswahl in der Kopfzeile |
| `reaper_panel_open` | Seitenpanel offen/zu |

Alle Zugriffe sind abgesichert; ohne localStorage funktioniert die GUI weiter.

## Mitgelieferte Bibliotheken

Unter `src/reaper_ai/web/vendor/`, ausgeliefert über `/vendor`, Lizenztexte daneben.
Keine CDN-Abhängigkeit, kein npm.

| Datei | Bibliothek | Lizenz |
|---|---|---|
| `marked.umd.js` | marked 15.0.12 | MIT |
| `purify.min.js` | DOMPurify 3.4.16 | Apache-2.0 / MPL-2.0 |
| `highlight.min.js`, `hljs-powershell.min.js`, `hljs-github.min.css`, `hljs-github-dark.min.css` | highlight.js 11.12.0 | BSD-3-Clause |

Aktualisieren: Dateien aus dem offiziellen Release der jeweiligen Bibliothek
übernehmen, Dateinamen beibehalten, Versionen in dieser Tabelle anpassen.
