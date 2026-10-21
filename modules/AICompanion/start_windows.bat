@echo off
cd /d "%~dp0..\.."
python -m modules.AICompanion.main %*
pause
