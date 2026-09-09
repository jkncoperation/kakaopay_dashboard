"""
카카오페이 광고센터 소재별 성과 수집기
--------------------------------------
- 로그인된 Chrome 프로필(./chrome-profile)로 광고센터에 접속해 소재 탭의 표를 읽어
  구글 시트의 KakaopayRAW 탭에 날짜별로 기록(같은 날짜는 덮어씀)합니다.
- 최초 1회: `python kakaopay_scraper.py --login` 으로 창을 띄워 카카오 로그인 후 창을 닫습니다.
- 이후:   `python kakaopay_scraper.py`               → 어제·오늘 수집
          `python kakaopay_scraper.py --days 7`      → 최근 7일
          `python kakaopay_scraper.py --start 2026-09-01 --end 2026-09-09`

필요 패키지: pip install playwright gspread google-auth
             playwright install chromium
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- 설정
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_CONFIG = {
    # 광고센터
    "ad_account_id": "10000246",
    "campaign_ids": ["10000919"],          # 비우면([]) 계정 전체 캠페인
    "page_size": 100,
    # 구글 시트 (수집 결과를 쓸 곳)
    "service_account_file": "service_account.json",
    "spreadsheet_id": "1qcffn2CLiEK4U13vr_dR95-kZq99xI9vkPY_hAJM6m4",
    "worksheet": "KakaopayRAW",
    # 브라우저
    "profile_dir": "chrome-profile",
    "headless": True,
}

HEADER = ["날짜", "소재", "ON/OFF", "상태", "광고그룹", "광고상품",
          "소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC", "전환수",
          "소재ID", "캠페인ID", "수집시각"]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def num(s: str):
    """'4,200원' → 4200, '0.11%' → 0.11, '' → 0"""
    s = (s or "").strip().replace(",", "").replace("원", "").replace("%", "")
    if s in ("", "-"):
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


# ---------------------------------------------------------------- 수집
def content_url(cfg: dict, day: str, campaign_id: str | None) -> str:
    base = (f"https://adcenter.kakaopay.com/ad-management/{cfg['ad_account_id']}/content"
            f"?startDate={day}&endDate={day}&size={cfg['page_size']}")
    if campaign_id:
        base += f"&campaignId={campaign_id}"
    return base


JS_READ_TABLE = """
() => {
  const rows = [...document.querySelectorAll('table tbody tr')];
  return rows.map(tr => {
    const tds = [...tr.querySelectorAll('td')].map(td => td.innerText.replace(/\\s+/g, ' ').trim());
    const sw = tr.querySelector('td:nth-child(4) input[type=checkbox], td:nth-child(4) [role=switch]');
    let onoff = '';
    if (sw) onoff = (sw.checked || sw.getAttribute('aria-checked') === 'true') ? 'ON' : 'OFF';
    return { tds, onoff };
  });
}
"""


def scrape_day(page, cfg: dict, day: str, campaign_id: str | None) -> list[list]:
    url = content_url(cfg, day, campaign_id)
    page.goto(url, wait_until="networkidle", timeout=60_000)
    # 로그인 페이지로 튕기면 중단
    if "accounts.kakao" in page.url or "login" in page.url.lower():
        raise RuntimeError("로그인이 풀렸습니다. `python kakaopay_scraper.py --login` 으로 다시 로그인해 주세요.")
    # 표가 뜰 때까지 대기 (데이터 없으면 빈 표)
    try:
        page.wait_for_selector("table tbody tr", timeout=15_000)
    except Exception:
        return []
    time.sleep(1.0)
    raw = page.evaluate(JS_READ_TABLE)
    out = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in raw:
        t = r["tds"]
        if len(t) < 14:
            continue
        # 열: 0체크, 1소재ID, 2소재, 3ON/OFF, 4상태, 5광고그룹, 6광고상품, 7소진, 8노출, 9클릭, 10클릭률, 11도달, 12eCPM, 13CPC, 14기간
        out.append([
            day, t[2], r["onoff"], t[4], t[5], t[6],
            num(t[7]), num(t[8]), num(t[9]), num(t[10]), num(t[11]), num(t[12]), num(t[13]), "",
            t[1], campaign_id or "", now,
        ])
    return out


def scrape(cfg: dict, days: list[str], login_only: bool = False) -> list[list]:
    profile = Path(cfg["profile_dir"]).resolve()
    profile.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            headless=False if login_only else cfg["headless"],
            viewport={"width": 1400, "height": 900},
            locale="ko-KR",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if login_only:
            page.goto(f"https://adcenter.kakaopay.com/ad-management/{cfg['ad_account_id']}/content")
            print("브라우저 창에서 카카오 로그인을 완료한 뒤, 광고 관리 화면이 보이면 이 창을 닫아 주세요.")
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            ctx.close()
            return []

        rows: list[list] = []
        campaigns = cfg["campaign_ids"] or [None]
        for day in days:
            for cid in campaigns:
                got = scrape_day(page, cfg, day, cid)
                print(f"{day} campaign={cid or 'ALL'}: {len(got)}개 소재")
                rows.extend(got)
        ctx.close()
    return rows


# ---------------------------------------------------------------- 시트 기록
def upsert_sheet(cfg: dict, rows: list[list], days: list[str]) -> None:
    creds = Credentials.from_service_account_file(
        cfg["service_account_file"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(cfg["spreadsheet_id"])
    try:
        ws = sh.worksheet(cfg["worksheet"])
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(cfg["worksheet"], rows=2000, cols=len(HEADER))
        ws.append_row(HEADER)

    existing = ws.get_all_values()
    if not existing:
        ws.append_row(HEADER)
        existing = [HEADER]
    header, body = existing[0], existing[1:]
    # 수집한 날짜의 기존 행은 제거하고(덮어쓰기), 나머지는 유지
    keep = [r for r in body if r and r[0] not in days]
    new_body = keep + rows
    new_body.sort(key=lambda r: (r[0], r[4] if len(r) > 4 else "", r[1] if len(r) > 1 else ""))

    ws.clear()
    ws.update("A1", [header if len(header) >= 14 else HEADER] + new_body, value_input_option="USER_ENTERED")
    print(f"시트 '{cfg['worksheet']}' 갱신: {len(rows)}행 기록 (총 {len(new_body)}행)")


# ---------------------------------------------------------------- 진입점
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="창을 띄워 카카오 로그인만 수행")
    ap.add_argument("--days", type=int, default=2, help="오늘 포함 최근 N일 (기본 2 = 어제·오늘)")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--dry", action="store_true", help="시트에 쓰지 않고 출력만")
    args = ap.parse_args()
    cfg = load_config()

    if args.login:
        scrape(cfg, [], login_only=True)
        return

    if args.start:
        s = dt.date.fromisoformat(args.start)
        e = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    else:
        e = dt.date.today()
        s = e - dt.timedelta(days=args.days - 1)
    days = [(s + dt.timedelta(d)).isoformat() for d in range((e - s).days + 1)]

    rows = scrape(cfg, days)
    if args.dry:
        for r in rows:
            print(r)
        return
    upsert_sheet(cfg, rows, days)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 스케줄러 로그용
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
