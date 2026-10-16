"""P4-Watchdog-Integrationstest: laedt exakt den Servercode (main.py), reduziert
STAGE_TIMEOUT_S im Speicher auf 10 s und faehrt uvicorn auf Port 8735.
Eigene Session, eigener Port; der Code-Pfad (Watchdog-Thread -> _terminate_live_stage
-> TerminateJobObject -> Worker-Finalisierung) ist identisch mit Produktion."""
import sys
sys.path.insert(0, r"C:\tools\reaper-api")
import main
main.STAGE_TIMEOUT_S = 10
import uvicorn
uvicorn.run(main.app, host="127.0.0.1", port=8735, log_level="info")
