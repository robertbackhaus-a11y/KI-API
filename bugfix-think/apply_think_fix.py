# -*- coding: utf-8 -*-
"""Bugfix Bug 1 (E2E 2026-10-16): MessageIn.think + --think on/off im Popen-argv.
Byte-genauer Patch (CRLF-erhaltend); jeder Anchor muss exakt 1x vorkommen."""
import ast
import pathlib

MAIN = pathlib.Path(r"C:\tools\reaper-api\main.py")
README = pathlib.Path(r"C:\tools\reaper-api\README.md")
CR = b"\r\n"


def patch(path: pathlib.Path, pairs: list[tuple[bytes, bytes]]) -> None:
    data = path.read_bytes()
    for i, (old, new) in enumerate(pairs, 1):
        n = data.count(old)
        if n != 1:
            raise SystemExit(f"FAIL {path.name} anchor#{i}: {n} Treffer (erwartet 1)")
        data = data.replace(old, new)
    path.write_bytes(data)
    print(f"OK {path.name}: {len(pairs)} Anker ersetzt")


patch(MAIN, [
    # 1) MessageIn-Docstring
    (b'"""Body von POST /sessions/{id}/messages. model/project sind uebersteuerbar;' + CR +
     b'    Defaults = lokal vorhandenes Ollama-Modell bzw. Standard-Memory-Projekt."""',
     b'"""Body von POST /sessions/{id}/messages. model/project/think sind uebersteuerbar;' + CR +
     b'    Defaults = lokal vorhandenes Ollama-Modell, Standard-Memory-Projekt, Reasoning AUS' + CR +
     b'    (deterministisch; True = Modell denkt mit -- bei Qwen3/Q5 praktisch unbegrenzt)."""'),
    # 2) think-Feld nach project
    (b'    project: str = Field(default=DEFAULT_PROJECT, min_length=1,' + CR +
     b'                         description=f"Basic-Memory-Projekt (Default {DEFAULT_PROJECT})")',
     b'    project: str = Field(default=DEFAULT_PROJECT, min_length=1,' + CR +
     b'                         description=f"Basic-Memory-Projekt (Default {DEFAULT_PROJECT})")' + CR +
     b'    # think wird IMMER explizit an stage_runner.py durchgereicht (--think on/off):' + CR +
     b'    # fehlt der Key, laeuft der Runner mit "auto" und das Modell nutzt seinen eigenen' + CR +
     b'    # Reasoning-Default (Qwen3/Q5: praktisch unbegrenzt) -- E2E-Bug, fix 2026-10-16.' + CR +
     b'    think: bool = Field(default=False,' + CR +
     b'                        description="Reasoning/Thinking des Modells (Ollama \'think\'); Default aus")'),
    # 3) Popen-argv
    (b'"--model", body.model, "--project", body.project,' + CR +
     b'             "--task", f"@{task_path}"],',
     b'"--model", body.model, "--project", body.project,' + CR +
     b'             "--think", "on" if body.think else "off",' + CR +
     b'             "--task", f"@{task_path}"],'),
])

patch(README, [
    # Request-Body-Beispiel
    (b'      "project": "coding-v2"               # optional, Default wie gezeigt (Basic-Memory-Projekt)' + CR +
     b'    }',
     b'      "project": "coding-v2",              # optional, Default wie gezeigt (Basic-Memory-Projekt)' + CR +
     b'      "think":   false                     # optional, Default false (Reasoning AUS); true =' + CR +
     b'                                           # Modell denkt mit (Ollama \'think\'); Qwen3/Q5 ohne' + CR +
     b'                                           # Angabe = eigener Modell-Default (praktisch unbegrenzt)' + CR +
     b'    }'),
    # Subprozess-Beschreibung
    (b'Subprozess mit --model/--project/--task @<datei> gestartet. stdout- und stderr-Pipe',
     b'Subprozess mit --model/--project/--think on|off/--task @<datei> gestartet. think wird' + CR +
     b"IMMER explizit durchgereicht (Default aus): ohne den Key wuerde der Runner sein 'auto'" + CR +
     b'nehmen und das Modell seinen eigenen Reasoning-Default nutzen (Qwen3/Q5: praktisch' + CR +
     b'unbegrenzt). stdout- und stderr-Pipe'),
])

ast.parse(MAIN.read_text(encoding="utf-8"))
print("OK main.py: ast.parse bestanden")