$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8734"
$outFile = "C:\tools\reaper-api\test-p4\abort-tree-results.txt"
$log = New-Object System.Collections.Generic.List[string]
function Say($s){ $log.Add("$s") }

Say "=== Abort-Test Prozessbaum inkl. pwsh-Kind (P4) $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
$r = Invoke-RestMethod -Uri "$base/sessions" -Method POST -TimeoutSec 10
$sid = $r.session_id
Say "Session: $sid"

$task = 'Fuehre mit dem Tool run_powershell und dem Parameter timeout_seconds=600 das PowerShell-Kommando "Start-Sleep -Seconds 300; Write-Output sleep-finished" aus. Warte zwingend auf den Abschluss des Tools, bevor du die Stage mit finish_stage beendest.'
$body = @{ message = $task; model = "qwen3:8b"; project = "coding-v2" } | ConvertTo-Json -Depth 5
$job = Start-Job -ScriptBlock {
    param($u, $b)
    $resp = Invoke-WebRequest -Uri $u -Method POST -Body $b -ContentType "application/json" -UseBasicParsing -TimeoutSec 600
    [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = ($resp.Content | ConvertFrom-Json) }
} -ArgumentList "$base/sessions/$sid/messages", $body

# Warten bis der pwsh-Kindprozess existiert (max. 90 s)
$stagePid = 0; $childPids = @()
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 3
    $st = (Invoke-RestMethod -Uri "$base/sessions/$sid" -TimeoutSec 10).stages | Where-Object status -eq running
    if (-not $st) { continue }
    $stagePid = [int]$st.pid
    $ch = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $stagePid })
    if ($ch.Count -gt 0) {
        $childPids = @($ch | ForEach-Object { [pscustomobject]@{ Pid=[int]$_.ProcessId; Name=$_.Name } })
        break
    }
    Say ("warte auf Kindprozess t=+{0}s stage-pid={1}" -f ($i*3), $stagePid)
}
if ($stagePid -eq 0 -or $childPids.Count -eq 0) { Stop-Job $job; Remove-Job $job -Force; Say "FEHLER: kein Kindprozess gefunden"; Set-Content $outFile ($log -join "`n"); Write-Output ($log -join "`n"); throw "kein pwsh-Kind" }
Say ("Stage-PID {0}, Kindprozesse: {1}" -f $stagePid, (($childPids | ForEach-Object { "{0}({1})" -f $_.Pid, $_.Name }) -join '; '))

$t0 = Get-Date
$resp = Invoke-WebRequest -Uri "$base/sessions/$sid/abort" -Method POST -UseBasicParsing -TimeoutSec 60
$ms = [math]::Round(((Get-Date) - $t0).TotalMilliseconds)
Say ("ABORT HTTP {0} nach {1} ms" -f [int]$resp.StatusCode, $ms)
$ab = ($resp.Content | ConvertFrom-Json).stage
Say "Abort-Response: status=$($ab.status) exit_code=$($ab.exit_code) duration_s=$($ab.duration_s)"

Start-Sleep -Seconds 2
$alive = @(Get-Process -Id $stagePid -ErrorAction SilentlyContinue)
Say "Stage-PID $stagePid nach Abort: " + $(if($alive){"LEBT NOCH!"}else{"tot"})
foreach ($c in $childPids) {
    $ca = @(Get-Process -Id $c.Pid -ErrorAction SilentlyContinue)
    Say ("Kind {0} ({1}) nach Abort: {2}" -f $c.Pid, $c.Name, $(if($ca){"LEBT NOCH!"}else{"tot"}))
}
$chAfter = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $stagePid })
Say "Kinder nach Abort (per ParentProcessId): " + $(if($chAfter){(($chAfter | ForEach-Object {$_.Name}) -join '; ')}else{'keine'})

$jr = Receive-Job $job -TimeoutSec 90
Remove-Job $job -Force
$stg = $jr.Body.stage
Say "Messages-POST (Hintergrund): HTTP $($jr.Status), status=$($stg.status) exit_code=$($stg.exit_code) duration_s=$($stg.duration_s)"
Say "=== FERTIG ==="
Set-Content -Path $outFile -Value ($log -join "`n") -Encoding utf8
Write-Output ($log -join "`n")
