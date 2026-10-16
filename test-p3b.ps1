$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8734"
$out = "C:\tools\reaper-api\test-p3"
$sid = Get-Content "$out\session-id.txt" -Raw
$ct = "application/json; charset=utf-8"

# A) Stage (echtes Modell, dauert > 20 s) im Hintergrund ...
$jobA = Start-Job -ScriptBlock {
    param($sid, $ct)
    $body = @{ message = "Sag auf Franzoesisch ein kurzes Hallo und ruf dann finish_stage auf." } | ConvertTo-Json -Compress
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8734/sessions/$sid/messages" -Method Post -ContentType $ct -Body $body -TimeoutSec 570
        [IO.File]::WriteAllText("C:\tools\reaper-api\test-p3\response-a.json", $r.Content, [Text.UTF8Encoding]::new($false))
        "A_OK"
    } catch { "A_FEHLER: $($_.Exception.Message)" }
} -ArgumentList $sid, $ct

Start-Sleep -Seconds 3  # A muss laeuft sein und die Session haelt

# B) sofort zweiter POST -> erwartet 409
try {
    $rB = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body (@{message="x"} | ConvertTo-Json -Compress) -TimeoutSec 60
    "B_UNERWARTET_OK: $($rB.StatusCode)"
} catch {
    "B_STATUS=[int]$($_.Exception.Response.StatusCode) (erwartet 409)"
    if ($_.Exception.Response) {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        [IO.File]::WriteAllText("$out\response-b-409.txt", $sr.ReadToEnd(), [Text.UTF8Encoding]::new($false))
    }
}

Wait-Job $jobA -Timeout 600 | Out-Null
Receive-Job $jobA | ForEach-Object { "JOB_A: $_" }
Remove-Job $jobA -Force

# C) Fehlerpfad: unbekanntes Modell -> Runner rc=1
$bodyC = @{ message = "Hallo."; model = "does-not-exist-xyz" } | ConvertTo-Json -Compress
try {
    $rC = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body $bodyC -TimeoutSec 120
    [IO.File]::WriteAllText("$out\response-c.json", $rC.Content, [Text.UTF8Encoding]::new($false))
    "C_OK (HTTP 200, Stage status=error erwartet)"
} catch { "C_FEHLER: $($_.Exception.Message)" }

# D) Validation: nur-Leerraum-Message -> erwartet 422
try {
    $rD = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType $ct -Body (@{message="   "} | ConvertTo-Json -Compress) -TimeoutSec 60
    "D_UNERWARTET_OK: $($rD.StatusCode)"
} catch { "D_STATUS=[int]$($_.Exception.Response.StatusCode) (erwartet 422)" }

$g = Invoke-RestMethod -Uri "$base/sessions/$sid" -TimeoutSec 30
$g | ConvertTo-Json -Depth 10 | Set-Content "$out\session-get-final.json"
Copy-Item "C:\tools\reaper-api\sessions\$sid.jsonl" "$out\session-raw-final.jsonl"
"DONE-B"