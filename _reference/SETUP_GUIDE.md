# 설정 가이드 (VS Code · Windows 기준)

전체 순서
1. 파이썬 + VS Code 준비
2. 프로젝트 폴더 열기 · 가상환경 · 패키지 설치
3. 구글 서비스 계정 만들기 → 시트 2개에 공유
4. 수집기 설정(config.json) → 카카오 로그인 → 테스트 실행 → 시트 기록
5. 대시보드 로컬 실행 (secrets.toml)
6. 작업 스케줄러로 수집 자동화
7. GitHub + Streamlit Cloud 배포 (팀 공유)

---

## 1. 파이썬 + VS Code 준비

1. 파이썬: https://www.python.org/downloads/ 에서 3.11 또는 3.12 설치.
   설치 첫 화면에서 **"Add python.exe to PATH" 체크** 후 Install Now.
2. VS Code: https://code.visualstudio.com/ 설치.
3. VS Code 실행 → 왼쪽 확장(Extensions, 네모 4개 아이콘) → `Python` (Microsoft) 설치.
4. 확인: VS Code 메뉴 `터미널 > 새 터미널` 열고
   ```
   python --version
   ```
   `Python 3.11.x` 같은 버전이 나오면 OK. (`python`이 안 되면 `py --version`)

## 2. 프로젝트 폴더 열기 · 가상환경 · 패키지 설치

1. 받은 `kakaopay_dashboard.zip` 을 원하는 위치에 풀기. 예: `C:\work\kakaopay_dashboard`
2. VS Code → `파일 > 폴더 열기` → 그 폴더 선택.
3. 터미널(`터미널 > 새 터미널`)에서 가상환경 만들고 켜기:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   ```
   프롬프트 앞에 `(.venv)` 가 붙으면 성공.
   - 만약 "이 시스템에서 스크립트를 실행할 수 없으므로…" 오류가 나면 한 번만:
     ```
     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
     ```
     입력하고 `Y`, 그 다음 다시 `.venv\Scripts\activate`.
4. VS Code 오른쪽 아래 파이썬 버전 표시를 클릭 → `.venv` 인터프리터 선택 (앞으로 실행이 이 환경에서 되도록).
5. 패키지 설치:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
   두 번째 명령은 수집기용 크롬을 내려받습니다(1~2분).

## 3. 구글 서비스 계정 (파이썬이 시트를 읽고 쓰기 위한 계정)

1. https://console.cloud.google.com 접속 (지금 시트 쓰는 구글 계정으로 로그인).
2. 상단 프로젝트 선택 → **새 프로젝트** → 이름 `kakaopay-dashboard` → 만들기 → 그 프로젝트 선택.
3. 왼쪽 메뉴 `API 및 서비스 > 라이브러리` → `Google Sheets API` 검색 → **사용**.
   같은 방법으로 `Google Drive API` 도 **사용** (gspread가 시트를 여는 데 필요).
4. `API 및 서비스 > 사용자 인증 정보` → 상단 **+ 사용자 인증 정보 만들기 > 서비스 계정**
   - 이름 `sheet-bot` → 만들고 계속 → 역할은 건너뛰어도 됨 → 완료.
5. 만들어진 서비스 계정 클릭 → 상단 **키** 탭 → `키 추가 > 새 키 만들기 > JSON` → 파일이 다운로드됨.
6. 그 파일 이름을 `service_account.json` 으로 바꿔서 **프로젝트 폴더에 넣기**
   (`.gitignore` 에 이미 등록되어 있어 GitHub에는 안 올라갑니다).
7. 파일을 메모장으로 열어 `"client_email": "sheet-bot@....iam.gserviceaccount.com"` 값을 복사.
8. 구글 시트 두 개를 열어 **공유** 버튼 → 그 이메일을 추가:
   - `신규 세트용 데이터 관리 시트` → **편집자** (수집기가 KakaopayRAW 탭에 씀)
   - `클로드 공유용 DB RAW` → **뷰어** (읽기만)

## 4. 수집기 설정 → 로그인 → 테스트

1. 폴더의 `config.example.json` 을 복사해서 `config.json` 으로 저장. 내용은 이미 지금 계정/캠페인 기준으로 채워져 있음:
   - `ad_account_id`: `10000246` (법무법인 평온)
   - `campaign_ids`: `["10000919"]` — 다른 캠페인도 모으려면 ID를 배열에 추가. 계정 전체는 `[]`.
   - `spreadsheet_id` / `worksheet`: 기록할 시트와 탭 (기본: 신규 세트용 시트의 `KakaopayRAW`)
2. 카카오 로그인 (최초 1회):
   ```
   python kakaopay_scraper.py --login
   ```
   크롬 창이 뜨면 카카오 계정으로 로그인 → 광고 관리 화면이 보이면 **그 창을 닫기**.
   로그인 상태는 폴더 안 `chrome-profile\` 에 저장됩니다(이 폴더도 GitHub 제외).
3. 읽히는지 확인 (시트에는 안 씀):
   ```
   python kakaopay_scraper.py --dry
   ```
   `2026-09-09 campaign=10000919: 39개 소재` 처럼 나오고 행 목록이 출력되면 성공.
4. 실제 기록:
   ```
   python kakaopay_scraper.py
   ```
   → KakaopayRAW 탭에 어제·오늘 행이 들어갑니다. 처음엔 과거도 채우려면
   ```
   python kakaopay_scraper.py --start 2026-09-03 --end 2026-09-09
   ```
5. 자주 나는 문제
   - `로그인이 풀렸습니다` → `--login` 다시 실행.
   - `PermissionError / 403` → 3-8단계 시트 공유가 안 된 것. 편집자로 공유했는지 확인.
   - `WorksheetNotFound` → config.json의 worksheet 이름 오타.

## 5. 대시보드 로컬 실행

1. `.streamlit\secrets.example.toml` 을 복사해서 `.streamlit\secrets.toml` 로 저장.
2. 메모장으로 열어 채우기:
   - `app_password = "팀비밀번호"` — 팀이 대시보드 열 때 입력할 비밀번호
   - `[gcp_service_account]` 아래 값들 → `service_account.json` 의 같은 키 값을 그대로 복사.
     `private_key` 는 따옴표 안에 한 줄로, 줄바꿈이 `\n` 으로 들어 있는 상태 그대로 붙여넣기.
3. 실행:
   ```
   streamlit run app.py
   ```
   브라우저가 자동으로 `http://localhost:8501` 을 엽니다. 비밀번호 입력 → 대시보드.
4. 사용법
   - 왼쪽: 기간(오늘/어제/최근 7일/이번 달/직접), 광고 세트 선택
   - CPC 수정 전 차감: 한 줄에 `채무조정_ad2 26400` 형식
   - DB 건수 직접 지정: 한 줄에 `채무조정_ad6 20` 형식 (시트 집계보다 우선)
   - 탭: 소재별 / 세트별 / 일별 추이 / 복사용 리포트(그대로 복사해서 채팅에 붙이기)
   - 시트를 바꾼 뒤 바로 보려면 왼쪽 아래 **데이터 새로고침** (기본 10분 캐시)
5. 종료: 터미널에서 `Ctrl + C`.

## 6. 수집 자동화 (Windows 작업 스케줄러)

1. 프로젝트 폴더에 `run_scraper.bat` 파일 만들기:
   ```
   @echo off
   cd /d C:\work\kakaopay_dashboard
   call .venv\Scripts\activate
   python kakaopay_scraper.py >> scraper.log 2>&1
   ```
   (경로는 실제 폴더로 수정)
2. 시작 메뉴에서 `작업 스케줄러` 실행 → 오른쪽 **기본 작업 만들기**
   - 이름 `kakaopay 수집`
   - 트리거 `매일` → 만든 뒤 작업 속성에서 트리거 편집 → `작업 반복 간격 1시간`, `기간 무기한` 체크
   - 동작 `프로그램 시작` → 프로그램: `C:\work\kakaopay_dashboard\run_scraper.bat`
   - 속성 > 일반: "사용자가 로그온되어 있을 때만 실행" 유지 (크롬 프로필 때문에 이게 안전)
3. `scraper.log` 를 열어 `시트 'KakaopayRAW' 갱신` 이 찍히는지 확인.
   로그에 `로그인이 풀렸습니다` 가 보이면 4-2 단계 다시.

## 7. 팀 공유용 배포 (GitHub + Streamlit Community Cloud, 무료)

1. GitHub 계정 준비 → https://github.com/new → 저장소 이름 `kakaopay-dashboard`, **Private** 선택 → Create.
2. VS Code 터미널에서 (프로젝트 폴더):
   ```
   git init
   git add .
   git commit -m "kakaopay dashboard"
   git branch -M main
   git remote add origin https://github.com/<내아이디>/kakaopay-dashboard.git
   git push -u origin main
   ```
   `.gitignore` 덕분에 `service_account.json`, `config.json`, `chrome-profile`, `secrets.toml` 은 올라가지 않습니다.
   (git이 없다고 나오면 https://git-scm.com/download/win 설치 후 VS Code 재시작)
3. https://share.streamlit.io → GitHub로 로그인 → **New app**
   - Repository: `kakaopay-dashboard`, Branch: `main`, Main file path: `app.py`
   - **Advanced settings > Secrets** 칸에 로컬 `.streamlit\secrets.toml` 내용을 그대로 붙여넣기
   - Deploy
4. 2~3분 뒤 `https://<앱이름>.streamlit.app` 링크가 생깁니다. 이 링크 + `app_password` 를 팀에 공유.
5. 코드 수정 후 반영은 `git add . && git commit -m "..." && git push` 만 하면 자동 재배포됩니다.
   Secrets 수정은 Streamlit Cloud 앱 화면 `Settings > Secrets` 에서.

---

## 파일 정리

| 파일 | 역할 | GitHub 업로드 |
|---|---|---|
| `kakaopay_scraper.py` | 광고센터 → 시트 수집기 (PC에서 실행) | O |
| `app.py` | Streamlit 대시보드 | O |
| `requirements.txt` | 패키지 목록 | O |
| `config.json` | 수집기 설정 (계정/캠페인/시트) | X |
| `service_account.json` | 구글 서비스 계정 키 | X |
| `.streamlit/secrets.toml` | 대시보드 비밀번호·구글 키 | X (Cloud Secrets에 붙여넣기) |
| `chrome-profile/` | 카카오 로그인 상태 | X |
