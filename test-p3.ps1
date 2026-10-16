$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8734"
$out = "C:\tools\reaper-api\test-p3"

# 1) Neue Session
$s = Invoke-RestMethod -Uri "$base/sessions" -Method Post -TimeoutSec 30
$sid = $s.session_id
Set-Content -Path "$out\session-id.txt" -Value $sid
Write-Output "SESSION=$sid"

# 2) Triviale Stage via POST /messages -- model/project bewusst NICHT gesetzt (Default-Pfad)
$body = @{ message = "Sag kurz Hallo auf Deutsch und ruf dann finish_stage auf." } | ConvertTo-Json -Compress
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $r = Invoke-WebRequest -Uri "$base/sessions/$sid/messages" -Method Post -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 570
    $sw.Stop()
    [IO.File]::WriteAllText("$out\response.json", $r.Content, [Text.UTF8Encoding]::new($false))
    Write-Output "HTTP_OK in $($sw.Elapsed.TotalSeconds)s"
} catch {
    $sw.Stop()
    Write-Output "HTTP_FEHLER nach $($sw.Elapsed.TotalSeconds)s: $($_.Exception.Message)"
    if ($_.Exception.Response) {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        [IO.File]::WriteAllText("$out\response-error.txt", $sr.ReadToEnd(), [Text.UTF8Encoding]::new($false))
    }
}

# 3) GET Session + Roh-JSONL zur Verifikation
$g = Invoke-RestMethod -Uri "$base/sessions/$sid" -TimeoutSec 30
$g | ConvertTo-Json -Depth 10 | Set-Content "$out\session-get.json"
Copy-Item "C:\tools\reaper-api\sessions\$sid.jsonl" "$out\session-raw.jsonl"
"TASKS_VERZ:"
Get-ChildItem C:\tools\reaper-api\tasks | Select-Object Name, Length
"DONE"