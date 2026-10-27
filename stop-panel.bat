@echo off
REM Sisyphus Panel - stop the background server (whatever is listening on port 7878).
setlocal
set "found="
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /r /c:":7878 .*LISTENING"') do (
    set "found=1"
    taskkill /PID %%p /T /F
)
if not defined found echo Sisyphus Panel is not running: nothing is listening on port 7878.
endlocal
