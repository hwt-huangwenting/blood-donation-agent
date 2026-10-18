@echo off
rem Blood-donation Agent launcher
cd /d %~dp0
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
