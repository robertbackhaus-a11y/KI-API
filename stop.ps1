# Reaper API (und optional Ollama) sauber stoppen -- Gegenstueck zu run.ps1.
$ErrorActionPreference = "Stop"

function Stop-ByPort([int]$port, [string]$label) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "$label laeuft nicht (Port $port frei)."
        return
    }
    foreach ($pid_ in ($conns.OwningProcess | Select-Object -Unique)) {
        try {
            # /T: ganzer Prozessbaum (Windows-Venv-Pattern dieser Maschine: venv
            # python.exe startet System-Python als Kindprozess, der den Port haelt).
            taskkill /PID $pid_ /T /F | Out-Null
            Write-Host "$label gestoppt (PID $pid_)."
        } catch {
            Write-Warning "$label (PID $pid_) konnte nicht beendet werden: $_"
        }
    }
}

Stop-ByPort -port 8734 -label "Reaper API"

if ($args -contains "-Ollama") {
    Stop-ByPort -port 11434 -label "Ollama"
} else {
    Write-Host "Ollama bleibt laufen (mit -Ollama auch stoppen)."
}
