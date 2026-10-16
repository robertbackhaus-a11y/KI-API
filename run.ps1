# Reaper API starten (eigene venv, nur Loopback 127.0.0.1:8734).
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "venv fehlt. Einmalig anlegen: python -m venv .venv und .venv\Scripts\pip install -r requirements.txt"
}
& $py -m uvicorn main:app --host 127.0.0.1 --port 8734 --app-dir $PSScriptRoot
