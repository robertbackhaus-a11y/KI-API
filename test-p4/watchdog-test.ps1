$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8735"
$outFile = "C:\tools\reaper-api\test-p4\watchdog-results.txt"
$log = New-Object System.Collections.Generic.List[string]
function Say($s){ $log.Add("$s") }
function GpuSample { ((nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits) -replace '\s+',' ') }

Say "=== Watchdog-Test (STAGE_TIMEOUT_S=10 s im Testprozess) $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
$r = Invoke-RestMethod -Uri "$base/sessions" -Method POST -TimeoutSec 10
$sid = $r.session_id
Say "Session: $sid"

$task = 'Schreibe ohne jede Tool-Nutzung einen ausfuehrlichen deutschen Abenteuerroman-Extrakt mit dem Titel "Der Turm am Rand der Welt": mindestens 3000 Woerter, in 10 Kapiteln, jedes Kapitel mit eigenen Dialogen und dichten Beschreibungen. Beginne direkt mit dem Roman.'
$body = @{ message = $task; model = "qwen3:8b"; project = "coding-v2" } | ConvertTo-Json -Depth 5
$t0 = Get-Date
$resp = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 300
$ms = [math]::Round(((Get-Date) - $t0).TotalMilliseconds)
Say ("Messages-POST HTTP {0} nach {1} ms (Watchdog soll ~10 s + Finalisierung brauchen)" -f [int]$resp.StatusCode, $ms)
$stg = ($resp.Content | ConvertFrom-Json).stage
Say "Stage: status=$($stg.status) exit_code=$($stg.exit_code) duration_s=$($stg.duration_s) pid=$($stg.pid)"
Say "Erwartet: status=watchdog_timeout, exit_code=null, duration_s knapp ueber 10"

Start-Sleep -Seconds 2
$alive = @(Get-Process -Id $stg.pid -ErrorAction SilentlyContinue)
Say "Stage-PID $($stg.pid) nach Abschluss: " + $(if($alive){"LEBT NOCH!"}else{"tot"})
$chAfter = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $stg.pid })
Say "Kindprozesse: " + $(if($chAfter){(($chAfter | ForEach-Object {$_.Name}) -join '; ')}else{'keine'})
Say "GPU nach Watchdog-Kill: $(GpuSample)"
$tail = ($stg.stderr_tail -split "`n" | Select-Object -Last 3) -join ' | '
Say "stderr-Tail Ende: $tail"
Say "=== FERTIG ==="
Set-Content -Path $outFile -Value ($log -join "`n") -Encoding utf8
Write-Output ($log -join "`n")
