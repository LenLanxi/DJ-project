@echo off
rem AI voice ground test launcher (bypasses PowerShell execution policy).
rem Real drone connected but takeoff is NEVER authorized: props must be removed.
rem Usage: start_voice_ground_test.cmd                  start the test
rem        start_voice_ground_test.cmd -ListDevices     list audio devices first
rem        start_voice_ground_test.cmd -InputDevice 5 -OutputDevice 7
rem        start_voice_ground_test.cmd -DryRun          self-check only
rem        start_voice_ground_test.cmd -MicTest         also really record 3s and check the mic level
rem        start_voice_ground_test.cmd -Loopback        playback->STT->intent self test (no speaking needed)
rem        start_voice_ground_test.cmd -Video           also open the video HUD window
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_voice_ground_test.ps1" %*
