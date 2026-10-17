# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Kommandozeile `reaper-ai`:

    reaper-ai run      [--host H] [--port P] [--data-dir D] [--ollama-url U]
                       [--runner-dir R] [--no-browser]
    reaper-ai stop     [--data-dir D]
    reaper-ai migrate  QUELLVERZEICHNIS [--data-dir D]
    reaper-ai --version

`run` startet die bestehende FastAPI-App (reaper_ai.main:app) mit uvicorn im
Vordergrund (einzelner Prozess, wie bisher run.ps1) und legt eine PID-Datei im
Datenverzeichnis ab; `stop` beendet genau diesen Prozess(baum). Der Stage-Runner
bleibt ein externer Subprozess und wird hier nur auf Vorhandensein geprueft.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__
from . import config

PID_FILE = "reaper-ai.pid"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _port_open(host: str, port: int, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _connect_host(host: str) -> str:
    # 0.0.0.0/:: lauschen auf allen Adressen -- fuer Browser/Pruefung Loopback verwenden.
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid_file(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / PID_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _resolve(args: argparse.Namespace) -> Path:
    """CLI-Optionen > Env-Vars > config.toml > Default; Ergebnis landet in os.environ,
    wo main.py es beim Import liest. Liefert das Datenverzeichnis."""
    if getattr(args, "data_dir", None):
        os.environ["REAPER_AI_HOME"] = str(Path(args.data_dir).expanduser().resolve())
    data_dir = config.data_dir()
    os.environ["REAPER_AI_HOME"] = str(data_dir)
    overrides = {
        "REAPER_HOST": getattr(args, "host", None),
        "REAPER_PORT": getattr(args, "port", None),
        "REAPER_OLLAMA_URL": getattr(args, "ollama_url", None),
        "REAPER_STAGE_RUNNER_DIR": getattr(args, "runner_dir", None),
    }
    for env, value in overrides.items():
        if value is not None:
            os.environ[env] = str(value)
    config.apply_config_to_env(config.load_config_file(data_dir))
    return data_dir


def cmd_run(args: argparse.Namespace) -> int:
    data_dir = _resolve(args)
    host = os.environ.get("REAPER_HOST") or config.DEFAULT_HOST
    port = int(os.environ.get("REAPER_PORT") or config.DEFAULT_PORT)
    url = f"http://{_connect_host(host)}:{port}/"

    info = _read_pid_file(data_dir)
    if info and _pid_alive(int(info.get("pid", 0))):
        print(f"Reaper-AI laeuft bereits (PID {info['pid']}, {info.get('url')}). "
              f"Beenden mit: reaper-ai stop", file=sys.stderr)
        return 1
    if _port_open(_connect_host(host), port):
        print(f"Port {port} ist bereits belegt -- anderen Port mit --port waehlen.", file=sys.stderr)
        return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    if host not in LOOPBACK_HOSTS:
        print(f"WARNUNG: Bind-Adresse {host} ist nicht Loopback. Reaper-AI hat keine "
              f"Authentifizierung und ist fuer lokalen Single-User-Betrieb gedacht.", file=sys.stderr)

    # Reihenfolge wie in main.py: Verzeichnis zuerst, Python/Skript einzeln uebersteuerbar.
    runner_dir = Path(os.environ.get("REAPER_STAGE_RUNNER_DIR") or config.DEFAULT_RUNNER_DIR)
    runner_py = Path(os.environ.get("REAPER_STAGE_PYTHON") or config.default_runner_python(runner_dir))
    runner_script = Path(os.environ.get("REAPER_STAGE_RUNNER_SCRIPT") or runner_dir / "stage_runner.py")
    o_host, o_port = config.ollama_host_port()

    print(f"Reaper-AI {__version__}")
    print(f"  GUI:          {url}")
    print(f"  Daten:        {data_dir}")
    print(f"  Stage-Runner: {runner_script}" + ("" if runner_script.exists() and runner_py.exists()
                                              else "  (NICHT GEFUNDEN -- Stages schlagen fehl)"))
    print(f"  Ollama:       {config.ollama_url()}" + ("" if _port_open(o_host, o_port, 1.0)
                                                    else "  (nicht erreichbar)"))

    pid_path = data_dir / PID_FILE
    pid_path.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port, "url": url}),
                        encoding="utf-8")

    if not args.no_browser:
        def _open_when_ready() -> None:
            for _ in range(40):
                time.sleep(0.5)
                if _port_open(_connect_host(host), port):
                    webbrowser.open(url)
                    return
        threading.Thread(target=_open_when_ready, daemon=True).start()

    import uvicorn

    try:
        uvicorn.run("reaper_ai.main:app", host=host, port=port, workers=1)
    finally:
        info = _read_pid_file(data_dir)
        if info and info.get("pid") == os.getpid():
            pid_path.unlink(missing_ok=True)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    data_dir = _resolve(args)
    pid_path = data_dir / PID_FILE
    info = _read_pid_file(data_dir)
    if not info:
        print(f"Reaper-AI laeuft nicht (keine PID-Datei in {data_dir}).")
        return 0
    pid = int(info.get("pid", 0))
    if not pid or not _pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        print("Reaper-AI laeuft nicht (veraltete PID-Datei entfernt).")
        return 0
    # Gegen PID-Wiederverwendung: nur beenden, wenn der eingetragene Port noch lauscht.
    if not _port_open(_connect_host(info.get("host", "")), int(info.get("port", 0))):
        print(f"PID {pid} lebt, aber Port {info.get('port')} lauscht nicht -- vermutlich "
              f"nicht Reaper-AI. Nichts beendet; PID-Datei entfernt.", file=sys.stderr)
        pid_path.unlink(missing_ok=True)
        return 1
    if sys.platform == "win32":
        # /T: ganzer Prozessbaum (wie stop.ps1); laufende Stages sterben ueber ihr
        # Job-Object (KILL_ON_JOB_CLOSE) mit und erscheinen nach dem Neustart als "incomplete".
        rc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, text=True).returncode
        if rc != 0:
            print(f"taskkill fuer PID {pid} fehlgeschlagen (rc={rc}).", file=sys.stderr)
            return 1
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.25)
    pid_path.unlink(missing_ok=True)
    print(f"Reaper-AI gestoppt (PID {pid}).")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Kopiert Sessions aus einem alten Checkout (<quelle>/sessions/*.jsonl) ins
    Datenverzeichnis. Nur kopieren: Quelle bleibt unveraendert, vorhandene Dateien im
    Ziel werden nie ueberschrieben."""
    data_dir = _resolve(args)
    src = Path(args.source).expanduser().resolve()
    src_sessions = src / "sessions" if (src / "sessions").is_dir() else src
    dst_sessions = data_dir / "sessions"
    if src_sessions.resolve() == dst_sessions.resolve():
        print("Quelle und Ziel sind identisch -- nichts zu tun.")
        return 0
    files = sorted(src_sessions.glob("*.jsonl"))
    if not files:
        print(f"Keine Sessions (*.jsonl) in {src_sessions} gefunden.", file=sys.stderr)
        return 1
    dst_sessions.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for f in files:
        target = dst_sessions / f.name
        if target.exists():
            skipped += 1
            continue
        shutil.copy2(f, target)
        copied += 1
    print(f"{copied} Session(s) nach {dst_sessions} kopiert, {skipped} bereits vorhanden "
          f"(nicht ueberschrieben). Quelle unveraendert.")
    if copied and _read_pid_file(data_dir):
        print("Hinweis: Reaper-AI neu starten, damit die Sessions geladen werden.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reaper-ai", description="Reaper-AI: lokale Chat-GUI fuer den Stage-Runner.")
    p.add_argument("--version", action="version", version=f"reaper-ai {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_data_dir(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--data-dir", help="Datenverzeichnis (Sessions, config.toml); "
                                           "Default: REAPER_AI_HOME bzw. %%LOCALAPPDATA%%\\reaper-ai")

    run = sub.add_parser("run", help="Server starten (Vordergrund) und GUI im Browser oeffnen")
    add_data_dir(run)
    run.add_argument("--host", help=f"Bind-Adresse (Default {config.DEFAULT_HOST})")
    run.add_argument("--port", type=int, help=f"Port (Default {config.DEFAULT_PORT})")
    run.add_argument("--ollama-url", help=f"Ollama-Endpunkt (Default {config.DEFAULT_OLLAMA_URL})")
    run.add_argument("--runner-dir", help=f"Verzeichnis von stage_runner.py (Default {config.DEFAULT_RUNNER_DIR})")
    run.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch oeffnen")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="laufenden Server beenden")
    add_data_dir(stop)
    stop.set_defaults(func=cmd_stop)

    mig = sub.add_parser("migrate", help="Sessions aus einem alten Checkout uebernehmen (nur kopieren)")
    mig.add_argument("source", help="altes Reaper-AI-Verzeichnis oder dessen sessions/-Ordner")
    add_data_dir(mig)
    mig.set_defaults(func=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
