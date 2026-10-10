@echo off
rem Double-click to set up and start Open WebUI with the Kitegate gateway.
rem Needs Docker Desktop running. Arguments are passed on, e.g. start.cmd -Local
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
