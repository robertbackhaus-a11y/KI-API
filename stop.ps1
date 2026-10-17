# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: GPL-3.0-or-later

# Cleanly stop the Reaper API (and optionally Ollama) -- counterpart to run.ps1.
$ErrorActionPreference = "Stop"

function Stop-ByPort([int]$port, [string]$label) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "$label is not running (port $port free)."
        return
    }
    foreach ($pid_ in ($conns.OwningProcess | Select-Object -Unique)) {
        try {
            # /T: whole process tree (Windows venv pattern on this machine: the venv
            # python.exe starts the system Python as a child process that holds the port).
            taskkill /PID $pid_ /T /F | Out-Null
            Write-Host "$label stopped (PID $pid_)."
        } catch {
            Write-Warning "$label (PID $pid_) could not be stopped: $_"
        }
    }
}

Stop-ByPort -port 8734 -label "Reaper API"

if ($args -contains "-Ollama") {
    Stop-ByPort -port 11434 -label "Ollama"
} else {
    Write-Host "Ollama keeps running (use -Ollama to stop it too)."
}
