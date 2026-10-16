$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8734"
$outFile = "C:\tools\reaper-api\test-p4\abort-gpu-results.txt"
$log = New-Object System.Collections.Generic.List[string]
function Say($s){ $log.Add("$s") }
function GpuSample { ((nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits) -replace '\s+',' ') }

Say "=== Abort-Test GPU (P4, fixiert main.py) $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
Say "GPU-Baseline: $(GpuSample)"

$r = Invoke-RestMethod -Uri "$base/sessions" -Method POST -TimeoutSec 10
$sid = $r.session_id
Say "Session: $sid"

$task = 'Schreibe ohne jede Tool-Nutzung einen ausfuehrlichen deutschen Abenteuerroman-Extrakt mit dem Titel "Der Turm am Rand der Welt": mindestens 3000 Woerter, in 10 Kapiteln, jedes Kapitel mit eigenen Dialogen und dichten Beschreibungen. Beginne direkt mit dem Roman.'
$body = @{ message = $task; model = "qwen3:8b"; project = "coding-v2" } | ConvertTo-Json -Depth 5
$job = Start-Job -ScriptBlock {
    param($u, $b)
    $resp = Invoke-WebRequest -Uri $u -Method POST -Body $b -ContentType "application/json" -UseBasicParsing -TimeoutSec 600
    [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = ($resp.Content | ConvertFrom-Json) }
} -ArgumentList "$base/sessions/$sid/messages", $body

$started = $false; $peak = 0
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 3
    $s = GpuSample; $util = [int]($s.Split(',')[0].Trim())
    Say ("wartet t=+{0,3}s util={1}% mem={2} MiB" -f ($i*3), $util, ($s.Split(',')[1].Trim()))
    if ($util -ge 15) { $started = $true }
    if ($util -gt $peak) { $peak = $util }
    if ($started -and $util -ge 30) { break }
}
if (-not $started) { Stop-Job $job; Remove-Job $job -Force; Say "FEHLER: keine GPU-Aktivitaet"; Set-Content $outFile ($log -join "`n"); Write-Output ($log -join "`n"); throw "keine Generation beobachtet" }

$st = (Invoke-RestMethod -Uri "$base/sessions/$sid" -TimeoutSec 10).stages | Where-Object status -eq running
Say "Stage: id=$($st.stage_id) pid=$($st.pid) peak-util-vorher=${peak}%"
$stagePid = [int]$st.pid
$before = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $stagePid } | ForEach-Object { "{0}({1})" -f $_.ProcessId, $_.Name })
Say "Kindprozesse vor Abort: " + $(if($before){$before -join '; '}else{'keine'})

$t0 = Get-Date
$resp = Invoke-WebRequest -Uri "$base/sessions/$sid/abort" -Method POST -UseBasicParsing -TimeoutSec 60
$ms = [math]::Round(((Get-Date) - $t0).TotalMilliseconds)
Say ("ABORT HTTP {0} nach {1} ms" -f [int]$resp.StatusCode, $ms)
$ab = ($resp.Content | ConvertFrom-Json).stage
Say "Abort-Response: status=$($ab.status) exit_code=$($ab.exit_code) duration_s=$($ab.duration_s)"

foreach ($d in 1,3,6,12) { Start-Sleep -Seconds $d; Say ("GPU nach Abort +{0}s: {1}" -f $d, (GpuSample)) }

$alive = @(Get-Process -Id $stagePid -ErrorAction SilentlyContinue)
Say "Stage-PID $stagePid nach Abort: " + $(if($alive){"LEBT NOCH!"}else{"tot"})
$chAfter = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $stagePid })
Say "Kindprozesse nach Abort: " + $(if($chAfter){(($chAfter | ForEach-Object {$_.Name}) -join '; ')}else{'keine'})

$wait = 0
while ($job.State -ne 'Completed' -and $wait -lt 60) { Start-Sleep -Seconds 1; $wait++ }
if ($job.State -ne "Completed") { Say "FEHLER: Messages-POST nach 60 s nicht abgeschlossen"; Stop-Job $job; Remove-Job $job -Force; Set-Content $outFile ($log -join "`n"); Write-Output ($log -join "`n"); throw "messages-post unvollendet" }
$jr = Receive-Job $job
Remove-Job $job -Force
$stg = $jr.Body.stage
Say "Messages-POST (Hintergrund): HTTP $($jr.Status), status=$($stg.status) exit_code=$($stg.exit_code) duration_s=$($stg.duration_s)"
$tail = ($stg.stderr_tail -split "`n" | Select-Object -Last 3) -join ' | '
Say "stderr-Tail Ende: $tail"
Say "=== FERTIG ==="
Set-Content -Path $outFile -Value ($log -join "`n") -Encoding utf8
Write-Output ($log -join "`n")
