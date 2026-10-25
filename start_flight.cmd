@echo off
rem Wrapper: bypasses PowerShell execution policy so this works by double-click or CLI.
rem Usage: start_flight.cmd            launch for real flight
rem        start_flight.cmd -DryRun    check config only, do not launch
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_flight.ps1" %*
