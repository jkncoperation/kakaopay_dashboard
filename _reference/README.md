# 카카오페이 광고 × DB 통합 대시보드

구성 (로그인은 내 PC, 대시보드는 클라우드)

```
[내 PC]  kakaopay_scraper.py ──(Playwright, 카카오 로그인 프로필)──▶ 광고센터 소재 표 읽기
                             └──(gspread)──▶ 구글 시트 KakaopayRAW 탭 (날짜별 소재 성과)
[클라우드] app.py (Streamlit) ──▶ KakaopayRAW + DB RAW 시트 읽기 → 소재별 소진·DB·DB단가·상태 집계
```

카카오 로그인(2단계 인증)은 클라우드 서버에서 안정적으로 유지하기 어려워서, 수집기는 PC에서 돌리고
결과를 시트에 쌓은 뒤 대시보드는 시트만 읽는 구조입니다. 팀은 대시보드 링크만 열면 됩니다.

## 1. 구글 서비스 계정 (한 번만)
1. https://console.cloud.google.com → 프로젝트 생성 → "API 및 서비스" → Google Sheets API 사용 설정
2. "사용자 인증 정보" → 서비스 계정 만들기 → 키 추가(JSON) → `service_account.json` 으로 저장
3. JSON 안의 `client_email` 주소를 두 시트(신규 세트용 데이터 관리 시트, 클로드 공유용 DB RAW)에
   편집자(수집용) / 뷰어(대시보드용)로 공유

## 2. 수집기 (내 PC, Windows)
```
pip install -r requirements.txt
playwright install chromium
copy config.example.json config.json      # 필요하면 수정
python kakaopay_scraper.py --login        # 창이 뜨면 카카오 로그인 후 창 닫기 (최초 1회)
python kakaopay_scraper.py --dry          # 어제·오늘 데이터가 읽히는지 확인
python kakaopay_scraper.py                # 시트에 기록
```
- 작업 스케줄러에 `python kakaopay_scraper.py` 를 1시간마다 등록하면 자동 갱신됩니다.
- 로그인이 풀리면 "로그인이 풀렸습니다" 에러가 나므로 `--login` 을 다시 실행합니다.
- `--start 2026-09-01 --end 2026-09-09` 로 과거 구간을 다시 채울 수 있습니다(같은 날짜는 덮어씀).
- KakaopayRAW 탭 열 순서는 기존 시트(날짜·소재·ON/OFF·상태·광고그룹·광고상품·소진비용·노출수·클릭수·클릭률·도달수·eCPM·CPC·전환수)와 같고
  뒤에 소재ID·캠페인ID·수집시각이 추가됩니다. 기존 보고서 수식은 그대로 동작합니다.

## 3. 대시보드
로컬 실행:
```
copy .streamlit\secrets.example.toml .streamlit\secrets.toml   # 값 채우기
streamlit run app.py
```
Streamlit Community Cloud 배포(팀 공유):
1. 이 폴더를 GitHub 비공개 저장소에 올림 (`.gitignore` 로 키 파일은 제외됨)
2. https://share.streamlit.io → New app → 저장소/`app.py` 선택
3. Settings → Secrets 에 `secrets.example.toml` 내용을 실제 값으로 붙여넣기 (`app_password` 가 팀 비밀번호)
4. 생성된 링크를 팀에 공유

## 4. 집계 규칙 (대시보드에 구현됨)
- DB utm_content `kakaopay_ad{세트}-{번호}` ↔ 소재명 `{접두어}{세트}_ad{번호}` (채무조정_ad6 = 세트1, 채무조정2_ad6 = 세트2)
- DB 단가 = 소진 ÷ DB (0건은 "-"), 미전환 소재도 모두 표시
- CPC 수정 전 지출 차감: 사이드바에 `소재명 금액` 입력 → 소재·합계 모두 차감
- DB 건수 직접 지정: 사이드바에 `소재명 건수` 입력 → 시트 집계보다 우선
- 상태 분류는 보고서 시트와 같은 정규식(진행불가 / 접수 / 미팅 / 승인)
- "복사용 리포트" 탭이 채팅에 붙이는 형식 그대로 생성
