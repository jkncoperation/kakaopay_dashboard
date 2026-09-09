@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0..\venv\Scripts\activate.bat"
streamlit run app.py
