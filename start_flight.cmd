@echo off
rem Same production entry for double-click/CLI. No console-focus manipulation.
rem Default leaves takeoff blocked. Use -AllowTakeoff only after the site is ready.
where pwsh >nul 2>nul
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_flight.ps1" %*
) else (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_flight.ps1" %*
)
exit /b %errorlevel%
