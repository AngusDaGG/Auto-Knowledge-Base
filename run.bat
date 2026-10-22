@echo off
cd /d "%~dp0"
python -X utf8 -B auto_kb.py %*
if "%~1"=="" pause
