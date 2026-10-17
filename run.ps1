# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

# Reaper API starten -- prueft/startet Ollama bei Bedarf, startet die API
# (eigene venv, nur Loopback 127.0.0.1:8734), oeffnet die GUI im Browser.
$ErrorActionPreference = "Stop"

$ollamaUrl = "http://127.0.0.1:11434/api/tags"
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"

try {
    Invoke-RestMethod -Uri $ollamaUrl -TimeoutSec 2 -ErrorAction Stop | Out-Null
    Write-Host "Ollama laeuft bereits."
} catch {
    if (-not (Test-Path $ollamaExe)) {
        throw "Ollama nicht gefunden unter $ollamaExe -- bitte manuell starten."
    }
    Write-Host "Ollama laeuft nicht -- starte es ..."
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
        throw "Ollama ist nach 15s nicht erreichbar -- bitte Log pruefen."
    }
    Write-Host "Ollama ist bereit."
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "venv fehlt. Einmalig anlegen: python -m venv .venv und .venv\Scripts\pip install -r requirements.txt"
}

# Browser erst oeffnen, wenn die API wirklich antwortet (sonst laedt die Seite ins Leere) --
# laeuft parallel als Job, waehrend uvicorn gleich im Vordergrund blockiert.
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
