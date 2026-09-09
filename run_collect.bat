@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0..\venv\Scripts\activate.bat"
python -X utf8 collect_all.py >> collect.log 2>&1
