$ErrorActionPreference = 'Stop'
$id = (Get-Content 'C:\tools\reaper-api\test-p5\session_p5.txt' -Raw).Trim()
Remove-Item 'C:\tools\reaper-api\test-p5\sse_stream.txt','C:\tools\reaper-api\test-p5\sse_headers.txt','C:\tools\reaper-api\test-p5\post_sse.done' -EA SilentlyContinue
curl.exe -N -sS -D 'C:\tools\reaper-api\test-p5\sse_headers.txt' -o 'C:\tools\reaper-api\test-p5\sse_stream.txt' -X POST "http://127.0.0.1:8734/sessions/$id/messages" -H 'Content-Type: application/json' -d '{"message": "Sag kurz Hallo und ruf dann finish_stage auf"}'
"curl exit: $LASTEXITCODE" | Out-File 'C:\tools\reaper-api\test-p5\post_sse.done' -Encoding ascii
