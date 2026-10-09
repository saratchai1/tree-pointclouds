@echo off
cd /d "%~dp0"
py -3 scripts\start_feedback.py
if errorlevel 1 pause
