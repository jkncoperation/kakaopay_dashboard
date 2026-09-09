@echo off
chcp 65001 >nul
REM 내 Chrome 을 원격 디버깅 옵션으로 켠다. 수집기가 이 Chrome 의 로그인 세션을 그대로 쓴다.
REM 주의: Chrome 이 이미 켜져 있으면 이 옵션이 안 먹는다. 먼저 Chrome 을 완전히 종료할 것.
tasklist /FI "IMAGENAME eq chrome.exe" 2>nul | find /I "chrome.exe" >nul
if not errorlevel 1 (
  echo [!] Chrome 이 실행 중입니다. 창을 모두 닫고 다시 실행해 주세요.
  echo     (작업 표시줄 우측 아이콘까지 종료해야 합니다^)
  pause
  exit /b 1
)
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
echo Chrome 을 디버깅 포트 9222 로 켰습니다. 평소처럼 쓰시면 됩니다.
echo 수집: python collect_db.py --cdp   /   python collect_live.py  (config.json 의 browser_mode 를 "cdp" 로)
timeout /t 5 >nul
