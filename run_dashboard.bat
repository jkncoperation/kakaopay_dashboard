@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0..\venv\Scripts\activate.bat"
echo.
echo  대시보드를 켭니다. 팀에게는 아래 'Network URL' 을 알려주세요.
echo  (같은 사무실 와이파이/랜에서 접속됩니다. 이 창을 닫으면 대시보드도 꺼집니다)
echo.
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
