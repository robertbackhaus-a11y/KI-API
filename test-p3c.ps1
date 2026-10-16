# Reaper API P3 Edge-Case-Tests (Nachfolger von test-p3.ps1 / test-p3b.ps1)
# Behebt zwei p3b-Bugs: (1) session-id wird getrimmt (kein %0D%0A in der URL),
# (2) Response-Body pwsh-7-kompatibel lesen (HttpResponseMessage.Content statt GetResponseStream).
$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8734"
$out = "C:\tools\reaper-api\test-p3"
$sid = (Get-Content "$out\session-id.txt" -Raw).Trim()
$ct = "application/json; charset=utf-8"
$resFile = "$out\results-p3c.txt"
Remove-Item $resFile -ErrorAction SilentlyContinue
Start-Transcript -Path "$out\run-c-transcript.log" -Append
$failures = 0

function Read-ErrBody($err) {
    try {
        $resp = $err.Exception.Response
        if ($null -eq $resp) { return "" }
        return $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    } catch { return "" }
}
function StatusCode($err) {
    try { [int]$err.Exception.Response.StatusCode } catch { -1 }
}
function Record([string]$name, [bool]$ok, [string]$detail) {
    $line = if ($ok) { "PASS  $name :: $detail" } else { "FAIL  $name :: $detail" }
    Write-Output $line
    Add-Content -Path $resFile -Value $line -Encoding utf8
    if (-not $ok) { $script:failures++ }
}

Write-Output "SID=$sid"

# E1: 422 -- ungueltiges JSON im Body
try {
    Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body '{invalid json' -TimeoutSec 30 | Out-Null
    Record "E1-malformed-json-422" $false "unerwartet HTTP 200"
} catch {
    $code = StatusCode $_
    [IO.File]::WriteAllText("$out\e1-body.txt", (Read-ErrBody $_), [Text.UTF8Encoding]::new($false))
    Record "E1-malformed-json-422" ($code -eq 422) "HTTP $code (erwartet 422)"
}

# E2: 404 -- POST messages auf unbekannte Session
try {
    Invoke-WebRequest -Uri "$base/sessions/keine-solche-session/messages" -Method Post -ContentType $ct -Body (@{message="x"} | ConvertTo-Json -Compress) -TimeoutSec 30 | Out-Null
    Record "E2-unknown-session-404" $false "unerwartet HTTP 200"
} catch {
    $code = StatusCode $_
    Record "E2-unknown-session-404" ($code -eq 404) "HTTP $code (erwartet 404)"
}

# E3: 409 -- Stage A laeuft, zweiter POST auf dieselbe Session sofort danach
$jobA = Start-Job -ScriptBlock {
    param($sid, $ct)
    $body = @{ message = "Sag auf Franzoesisch ein kurzes Hallo und ruf dann finish_stage auf." } | ConvertTo-Json -Compress
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8734/sessions/$sid/messages" -Method Post -ContentType $ct -Body $body -TimeoutSec 570
        [IO.File]::WriteAllText("C:\tools\reaper-api\test-p3\e3a-response.json", $r.Content, [Text.UTF8Encoding]::new($false))
        $j = $r.Content | ConvertFrom-Json
        "A_OK status=$($j.stage.status) rc=$($j.stage.exit_code)"
    } catch { "A_FEHLER: $($_.Exception.Message)" }
} -ArgumentList $sid, $ct
Start-Sleep -Seconds 3
try {
    $rB = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body (@{message="x"} | ConvertTo-Json -Compress) -TimeoutSec 60
    Record "E3-concurrent-409" $false "unerwartet HTTP $($rB.StatusCode)"
} catch {
    $code = StatusCode $_
    [IO.File]::WriteAllText("$out\e3-body-409.txt", (Read-ErrBody $_), [Text.UTF8Encoding]::new($false))
    Record "E3-concurrent-409" ($code -eq 409) "HTTP $code (erwartet 409)"
}
Wait-Job $jobA -Timeout 600 | Out-Null
$aOut = (Receive-Job $jobA) -join " "
Remove-Job $jobA -Force
Record "E3a-stage-a-done" ($aOut -match "A_OK") "Job-A: $aOut"

# E4: Fehlerpfad -- unbekanntes Modell -> Runner rc=1 -> HTTP 200, stage.status=error
$bodyC = @{ message = "Hallo."; model = "does-not-exist-xyz" } | ConvertTo-Json -Compress
try {
    $rC = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body $bodyC -TimeoutSec 180
    [IO.File]::WriteAllText("$out\e4-response.json", $rC.Content, [Text.UTF8Encoding]::new($false))
    $j = $rC.Content | ConvertFrom-Json
    Record "E4-errorpath-rc1" (($j.stage.status -eq "error") -and ($j.stage.exit_code -eq 1)) "HTTP 200, status=$($j.stage.status), exit_code=$($j.stage.exit_code) (erwartet error/1)"
} catch {
    Record "E4-errorpath-rc1" $false "unerwarteter HTTP-Fehler: code=$(StatusCode $_) / $($_.Exception.Message)"
}

# E5: 422 -- nur-Leerraum-Message
try {
    Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body (@{message="   "} | ConvertTo-Json -Compress) -TimeoutSec 30 | Out-Null
    Record "E5-blank-message-422" $false "unerwartet HTTP 200"
} catch {
    $code = StatusCode $_
    Record "E5-blank-message-422" ($code -eq 422) "HTTP $code (erwartet 422)"
}

# E6: Endzustand -- GET + JSONL-Zeilen + tasks/
$g = Invoke-RestMethod -Uri "$base/sessions/$sid" -TimeoutSec 30
$g | ConvertTo-Json -Depth 10 | Set-Content "$out\session-get-p3c.json"
Copy-Item "C:\tools\reaper-api\sessions\$sid.jsonl" "$out\session-raw-p3c.jsonl"
$lines = (Get-Content "C:\tools\reaper-api\sessions\$sid.jsonl").Count
$taskCount = @(Get-ChildItem C:\tools\reaper-api\tasks -Force).Count
$statuses = ($g.stages | ForEach-Object { $_.status }) -join ","
Record "E6-final-state" (($g.messages.Count -eq 3) -and ($g.stages.Count -eq 3) -and ($lines -eq 10) -and ($taskCount -eq 0)) "messages=$($g.messages.Count) stages=$($g.stages.Count) jsonl-zeilen=$lines task-dateien=$taskCount (erwartet 3/3/10/0); stage-status: $statuses"

if ($failures -eq 0) { Write-Output "ALL_PASS" } else { Write-Output "FAILURES: $failures" }
Add-Content -Path $resFile -Value "DONE-C failures=$failures" -Encoding utf8
Stop-Transcript
"DONE-C"
