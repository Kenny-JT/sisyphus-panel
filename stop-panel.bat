@echo off
REM Sisyphus Panel - stop the background server (whatever is listening on port 7878).
setlocal
set "found="
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /r /c:":7878 .*LISTENING"') do (
    set "found=1"
    REM Not /T: a Chrome that panel.py opened at start (open_page) is a child of the
    REM server, and when Chrome wasn't running yet, that child is the whole browser.
    REM The py launcher and the cmd above the server exit with it on their own.
    taskkill /PID %%p /F
)
if not defined found echo Sisyphus Panel is not running: nothing is listening on port 7878.
endlocal
