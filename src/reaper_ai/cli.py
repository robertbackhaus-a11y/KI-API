# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line `reaper-ai`:

    reaper-ai run      [--host H] [--port P] [--data-dir D] [--ollama-url U]
                       [--no-browser]
    reaper-ai stop     [--data-dir D]
    reaper-ai migrate  SOURCE_DIR [--data-dir D]
    reaper-ai --version

`run` starts the existing FastAPI app (reaper_ai.main:app) with uvicorn in the
foreground (single process, as run.ps1 did before) and writes a PID file into the
data directory; `stop` terminates exactly that process (tree). The stage runner is
part of the package (reaper_ai.runner.stage_runner); the API starts it per stage as
a separate subprocess -- it is never imported or started here.
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
    # 0.0.0.0/:: listen on all addresses -- use loopback for the browser/checks.
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
    """CLI options > env vars > config.toml > default; the result ends up in
    os.environ, where main.py reads it on import. Returns the data directory."""
    if getattr(args, "data_dir", None):
        os.environ["REAPER_AI_HOME"] = str(Path(args.data_dir).expanduser().resolve())
    data_dir = config.data_dir()
    os.environ["REAPER_AI_HOME"] = str(data_dir)
    overrides = {
        "REAPER_HOST": getattr(args, "host", None),
        "REAPER_PORT": getattr(args, "port", None),
        "REAPER_OLLAMA_URL": getattr(args, "ollama_url", None),
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
        print(f"Reaper-AI is already running (PID {info['pid']}, {info.get('url')}). "
              f"Stop it with: reaper-ai stop", file=sys.stderr)
        return 1
    if _port_open(_connect_host(host), port):
        print(f"Port {port} is already in use -- choose another port with --port.", file=sys.stderr)
        return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    if host not in LOOPBACK_HOSTS:
        print(f"WARNING: bind address {host} is not loopback. Reaper-AI has no "
              f"authentication and is intended for local single-user operation.", file=sys.stderr)

    # Same resolution as in main.py (config.runner_command).
    runner_py, runner_args, _ = config.runner_command(data_dir)
    if config.runner_is_integrated():
        runner_label = f"integrated ({config.RUNNER_MODULE})"
        runner_ok = runner_py.exists() and (Path(__file__).resolve().parent / "runner" / "stage_runner.py").exists()
    else:
        runner_label = f"external script {runner_args[0]}"
        runner_ok = runner_py.exists() and Path(runner_args[0]).exists()
    if os.environ.get("REAPER_STAGE_RUNNER_DIR"):
        print("WARNING: REAPER_STAGE_RUNNER_DIR is obsolete and ignored (the stage runner is "
              "integrated; set REAPER_STAGE_RUNNER_SCRIPT only to run an external runner).",
              file=sys.stderr)
    o_host, o_port = config.ollama_host_port()
    # R08: a regular, non-packaged PowerShell 7 is required for shell execution.
    # This is only a startup check with a clear hint -- the server still starts
    # (so --version/--help and the GUI work and a pip install needs no PowerShell);
    # the actual guard is in the runner, where run_powershell refuses to launch an
    # uncontained shell. No automatic installation.
    pwsh = config.resolve_powershell()

    print(f"Reaper-AI {__version__}")
    print(f"  GUI:          {url}")
    print(f"  Data:         {data_dir}")
    print(f"  Stage runner: {runner_label}" + ("" if runner_ok else "  (NOT FOUND -- stages will fail)"))
    print(f"  PowerShell:   {pwsh if pwsh else 'NOT FOUND -- stages that run commands will fail'}")
    print(f"  Ollama:       {config.ollama_url()}" + ("" if _port_open(o_host, o_port, 1.0)
                                                    else "  (not reachable)"))
    if not pwsh:
        print("\nWARNING: no regular (non-packaged) PowerShell 7 was found. The Microsoft\n"
              "Store/MSIX PowerShell is not used because it escapes the Windows job object\n"
              "(abort/watchdog could leave shell processes running). Install PowerShell 7:\n"
              "    winget install --id Microsoft.PowerShell --source winget\n"
              "Until then, commands via run_powershell will fail with a clear error; the\n"
              "server, the GUI and reaper-ai --version/--help work regardless.",
              file=sys.stderr)

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
        print(f"Reaper-AI is not running (no PID file in {data_dir}).")
        return 0
    pid = int(info.get("pid", 0))
    if not pid or not _pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        print("Reaper-AI is not running (stale PID file removed).")
        return 0
    # Guard against PID reuse: only terminate if the recorded port is still listening.
    if not _port_open(_connect_host(info.get("host", "")), int(info.get("port", 0))):
        print(f"PID {pid} is alive, but port {info.get('port')} is not listening -- probably "
              f"not Reaper-AI. Nothing terminated; PID file removed.", file=sys.stderr)
        pid_path.unlink(missing_ok=True)
        return 1
    if sys.platform == "win32":
        # /T: whole process tree (like stop.ps1); running stages die along with it via
        # their job object (KILL_ON_JOB_CLOSE) and show up as "incomplete" after a restart.
        rc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, text=True).returncode
        if rc != 0:
            print(f"taskkill for PID {pid} failed (rc={rc}).", file=sys.stderr)
            return 1
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.25)
    pid_path.unlink(missing_ok=True)
    print(f"Reaper-AI stopped (PID {pid}).")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Copies sessions from an old checkout (<source>/sessions/*.jsonl) into the
    data directory. Copy only: the source stays unchanged, existing files in the
    target are never overwritten."""
    data_dir = _resolve(args)
    src = Path(args.source).expanduser().resolve()
    src_sessions = src / "sessions" if (src / "sessions").is_dir() else src
    dst_sessions = data_dir / "sessions"
    if src_sessions.resolve() == dst_sessions.resolve():
        print("Source and target are identical -- nothing to do.")
        return 0
    files = sorted(src_sessions.glob("*.jsonl"))
    if not files:
        print(f"No sessions (*.jsonl) found in {src_sessions}.", file=sys.stderr)
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
    print(f"{copied} session(s) copied to {dst_sessions}, {skipped} already present "
          f"(not overwritten). Source unchanged.")
    if copied and _read_pid_file(data_dir):
        print("Note: restart Reaper-AI so the sessions are loaded.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reaper-ai", description="Reaper-AI: local chat GUI with integrated stage runner.")
    p.add_argument("--version", action="version", version=f"reaper-ai {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_data_dir(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--data-dir", help="data directory (sessions, config.toml); "
                                           "default: REAPER_AI_HOME or %%LOCALAPPDATA%%\\reaper-ai")

    run = sub.add_parser("run", help="start the server (foreground) and open the GUI in the browser")
    add_data_dir(run)
    run.add_argument("--host", help=f"bind address (default {config.DEFAULT_HOST})")
    run.add_argument("--port", type=int, help=f"port (default {config.DEFAULT_PORT})")
    run.add_argument("--ollama-url", help=f"Ollama endpoint (default {config.DEFAULT_OLLAMA_URL})")
    run.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="stop the running server")
    add_data_dir(stop)
    stop.set_defaults(func=cmd_stop)

    mig = sub.add_parser("migrate", help="import sessions from an old checkout (copy only)")
    mig.add_argument("source", help="old Reaper-AI directory or its sessions/ folder")
    add_data_dir(mig)
    mig.set_defaults(func=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
