@echo off
REM Sisyphus Panel - one-click start
REM Uses Windows native Python via py launcher (-3.12 pins to Windows Python, avoids msys2)
cd /d "%~dp0"
py -3.12 panel.py
pause
