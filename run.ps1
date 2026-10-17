# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

# Start the Reaper API -- checks/starts Ollama if needed, starts the API
# (own venv, loopback only 127.0.0.1:8734), opens the GUI in the browser.
$ErrorActionPreference = "Stop"

$ollamaUrl = "http://127.0.0.1:11434/api/tags"
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"

try {
    Invoke-RestMethod -Uri $ollamaUrl -TimeoutSec 2 -ErrorAction Stop | Out-Null
    Write-Host "Ollama is already running."
} catch {
    if (-not (Test-Path $ollamaExe)) {
        throw "Ollama not found at $ollamaExe -- please start it manually."
    }
    Write-Host "Ollama is not running -- starting it ..."
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    $ready = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod -Uri $ollamaUrl -TimeoutSec 2 -ErrorAction Stop | Out-Null
            $ready = $true
            break
        } catch { }
    }
    if (-not $ready) {
        throw "Ollama is not reachable after 15s -- please check the log."
    }
    Write-Host "Ollama is ready."
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "venv missing. Create it once: python -m venv .venv and .venv\Scripts\pip install -r requirements.txt"
}

# Only open the browser once the API really answers (otherwise the page loads into nothing) --
# runs in parallel as a job while uvicorn blocks in the foreground.
Start-Job -ScriptBlock {
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:8734/" -TimeoutSec 1 -ErrorAction Stop | Out-Null
            Start-Process "http://127.0.0.1:8734/"
            return
        } catch { }
    }
} | Out-Null

& $py -m uvicorn main:app --host 127.0.0.1 --port 8734 --app-dir $PSScriptRoot
