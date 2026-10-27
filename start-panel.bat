@echo off
REM Sisyphus Panel - foreground start in a visible console (handy for watching the log live).
REM For everyday use run start-panel.vbs instead: no window, no taskbar button.
REM Uses Windows native Python via py launcher (-3.12 pins to Windows Python, avoids msys2)
cd /d "%~dp0"
py -3.12 panel.py
pause
