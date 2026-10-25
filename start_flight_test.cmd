@echo off
rem Real-flight test launcher (bypasses PowerShell execution policy).
rem Usage: start_flight_test.cmd            start the test
rem        start_flight_test.cmd -DryRun   preflight checks only
rem        start_flight_test.cmd -Hybrid   lateral+yaw steering config
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_flight_test.ps1" %*
