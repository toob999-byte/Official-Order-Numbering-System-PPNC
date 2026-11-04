@echo off
chcp 65001 >nul
title GDMS - ระบบจัดเก็บคำสั่งและประกาศศาล (LAN Server)
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" run_server.py
) else (
    python run_server.py
)
pause
