"""실시간 수집 - 로그인된 크롬 프로필로 광고센터 소재 표를 직접 읽는다.

    python collect_live.py --login          최초 1회 (창이 뜨면 카카오 로그인 후 창 닫기)
    python collect_live.py                  오늘 수집 → data/ad_daily.sqlite
    python collect_live.py --date 2026-09-08
    python collect_live.py --start 2026-09-01 --end 2026-09-09
    python collect_live.py --dry            저장하지 않고 출력만

카카오 로그인은 2단계 인증 때문에 세션을 유지해야 해서 persistent profile(chrome-profile/)을 쓴다.
표는 화면 헤더 이름으로 읽으므로 열 순서가 바뀌어도 따라간다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

from core.browser import browser_context, first_page, restore_state, save_state
from parsers.adcenter_file import normalize_raw
from store import upsert_ad

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
# 로그인 직후의 세션(쿠키·localStorage). 인증 정보이므로 .gitignore 에 등록돼 있다.
STATE_PATH = HERE / "kakao_state.json"

DEFAULT_CONFIG = {
    "ad_account_id": "10000246",
    "campaign_ids": ["10000919"],      # 비우면 [] → 계정 전체
    "page_size": 100,
    "profile_dir": "chrome-profile",
    "headless": True,
    # "profile" = 전용 프로필(로그인 1회) / "cdp" = 이미 켜진 내 Chrome 에 붙기(로그인 불필요)
    "browser_mode": "profile",
    "cdp_url": "http://127.0.0.1:9222",
}

# 표의 헤더와 각 행 셀을 그대로 긁어온다. ON/OFF 는 토글 상태라 텍스트로 안 나와 별도 수집.
JS_READ_TABLE = """
() => {
  const table = document.querySelector('table');
  if (!table) return {head: [], rows: []};
  const head = [...table.querySelectorAll('thead th')].map(th => th.innerText.trim());
  const rows = [...table.querySelectorAll('tbody tr')].map(tr => {
    const tds = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
    const sw = tr.querySelector('td:not(:first-child) input[type=checkbox], td:not(:first-child) [role=switch]');
    let onoff = '';
    if (sw) onoff = (sw.checked || sw.getAttribute('aria-checked') === 'true') ? 'ON' : 'OFF';
    return {tds, onoff};
  });
  return {head, rows};
}
"""


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def content_url(cfg: dict, day: str, campaign_id: str | None) -> str:
    url = (f"https://adcenter.kakaopay.com/ad-management/{cfg['ad_account_id']}/content"
           f"?startDate={day}&endDate={day}&size={cfg.get('page_size', 100)}")
    if campaign_id:
        url += f"&campaignId={campaign_id}"
    return url


def table_to_df(head: list[str], rows: list[dict]) -> pd.DataFrame:
    """화면에서 읽은 표 → 헤더 있는 DataFrame (정규화는 normalize_raw 가 담당).

    맨 앞에 행 선택 체크박스 칸이 있어 td 가 헤더보다 하나 많은 경우를 보정한다.
    """
    if not rows:
        return pd.DataFrame()
    width = max(len(r["tds"]) for r in rows)
    shift = 1 if (head and width == len(head) + 1) else 0
    cols = list(head) if head else [f"c{i}" for i in range(width - shift)]

    data = []
    for r in rows:
        tds = r["tds"][shift:]
        tds = (tds + [""] * len(cols))[:len(cols)]
        data.append(tds)
    df = pd.DataFrame(data, columns=cols)
    df["ON/OFF"] = [r["onoff"] for r in rows]
    return df



def _wait_for_table(page, timeout_sec: int = 30):
    """표에 실제 데이터가 찰 때까지 기다린다.

    `table tbody tr` 이 생기는 순간에 읽으면 로딩 스켈레톤(빈 행)을 읽어 0건이 된다.
    소재명이 든 행이 하나라도 보이고, 행 수가 더 늘지 않을 때까지 기다린다.
    """
    import time as _time
    deadline = _time.time() + timeout_sec
    last_n, stable = -1, 0
    while _time.time() < deadline:
        try:
            data = page.evaluate(JS_READ_TABLE)
        except Exception:
            page.wait_for_timeout(500)
            continue
        rows = data.get("rows") or []
        filled = [r for r in rows if any(str(t).strip() for t in r.get("tds", []))]
        if filled:
            n = len(filled)
            stable = stable + 1 if n == last_n else 0
            last_n = n
            if stable >= 2:                 # 두 번 연속 같은 행 수면 다 그려진 것으로 본다
                return data
        page.wait_for_timeout(700)
    return data if (data.get("rows") if isinstance(data, dict) else None) else None


def _wait_for_login(page, timeout_sec: int = 600) -> bool:
    """광고 관리 화면에 표가 보일 때까지 기다린다. 사용자가 창을 닫으면 중단."""
    import time as _time
    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        try:
            if page.is_closed():
                return False
            url = page.url
            if "adcenter.kakaopay.com" in url and "accounts.kakao" not in url:
                if page.locator("table").count() > 0:
                    page.wait_for_timeout(1500)      # 쿠키가 기록될 여유
                    return True
            page.wait_for_timeout(2000)
        except Exception:
            return False
    return False


def check_login(cfg: dict | None = None) -> tuple[bool, str]:
    """저장된 프로필로 광고센터에 들어가지는지 확인."""
    cfg = cfg or load_config()
    url = content_url(cfg, dt.date.today().isoformat(),
                      (cfg.get("campaign_ids") or [None])[0])
    try:
        with browser_context(mode=cfg.get("browser_mode", "profile"),
                             profile_dir=HERE / cfg.get("profile_dir", "chrome-profile"),
                             headless=True,
                             cdp_url=cfg.get("cdp_url", "http://127.0.0.1:9222")) as ctx:
            restore_state(ctx, STATE_PATH)
            page = first_page(ctx)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            if "accounts.kakao" in page.url:
                return False, "로그인 안 됨 - 카카오 로그인 페이지로 넘어갑니다."
            n = page.locator("table tbody tr").count()
            return True, f"로그인 OK - 광고 관리 화면 접근됨 (표 {n}행)"
    except Exception as exc:
        return False, f"확인 실패: {exc}"


def collect(days: list[str], cfg: dict | None = None, login_only: bool = False) -> pd.DataFrame:
    cfg = cfg or load_config()
    mode = "profile" if login_only else cfg.get("browser_mode", "profile")

    frames: list[pd.DataFrame] = []
    with browser_context(mode=mode,
                         profile_dir=HERE / cfg.get("profile_dir", "chrome-profile"),
                         headless=False if login_only else bool(cfg.get("headless", True)),
                         cdp_url=cfg.get("cdp_url", "http://127.0.0.1:9222")) as ctx:
        if not login_only:
            nc, nl = restore_state(ctx, STATE_PATH)
            if nc:
                print(f"저장된 세션 주입: 쿠키 {nc}개" + (f", localStorage {nl}개" if nl else ""))
        page = first_page(ctx)

        if login_only:
            page.goto(f"https://adcenter.kakaopay.com/ad-management/{cfg['ad_account_id']}/content")
            print("브라우저에서 카카오 로그인을 해주세요.")
            print("  * '로그인 상태 유지' 를 반드시 체크하세요. 체크하지 않으면 창을 닫는 순간")
            print("    세션이 사라져서 다음 수집 때 다시 로그인해야 합니다.")
            print("  * 로그인이 확인되면 창이 자동으로 닫힙니다. (최대 10분 대기)")
            if _wait_for_login(page, timeout_sec=600):
                n = save_state(ctx, STATE_PATH)
                print("\n로그인 확인됨. 세션 쿠키 %d개를 저장했습니다." % n)
                print("(카카오 로그인 쿠키는 창을 닫으면 사라지는 세션 쿠키라 따로 보관합니다)")
            else:
                print("\n로그인을 확인하지 못했습니다. 다시 시도해 주세요.")
            return pd.DataFrame()

        campaigns = cfg.get("campaign_ids") or [None]
        for day in days:
            for cid in campaigns:
                page.goto(content_url(cfg, day, cid), wait_until="networkidle", timeout=60_000)
                if "accounts.kakao" in page.url or "login" in page.url.lower():
                    raise RuntimeError(
                        "로그인이 풀렸습니다. `python collect_live.py --login` 으로 다시 로그인해 주세요.")
                data = _wait_for_table(page)
                if data is None:
                    print(f"{day} campaign={cid or 'ALL'}: 표가 채워지지 않았습니다(데이터 없음).")
                    continue
                raw = table_to_df(data.get("head", []), data.get("rows", []))
                if raw.empty:
                    print(f"{day} campaign={cid or 'ALL'}: 0개 소재")
                    continue
                res = normalize_raw(raw, date=dt.date.fromisoformat(day), 출처="live")
                for w in res["warnings"]:
                    print(f"  ! {w}")
                print(f"{day} campaign={cid or 'ALL'}: {len(res['df'])}개 소재, "
                      f"소진 {res['df']['소진비용'].sum():,.0f}원")
                frames.append(res["df"])

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()



def _safe_console() -> None:
    """윈도우 cp949 콘솔에서 인코딩 못 하는 글자가 있어도 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="창을 띄워 카카오 로그인만 수행")
    ap.add_argument("--date", help="하루만 수집 (YYYY-MM-DD)")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--dry", action="store_true", help="저장하지 않고 출력만")
    ap.add_argument("--check", action="store_true", help="로그인 상태만 확인")
    args = ap.parse_args()

    cfg = load_config()
    if args.check:
        ok, msg = check_login(cfg)
        print(("[OK  ] " if ok else "[안됨] ") + msg)
        if not ok:
            print("  -> python collect_live.py --login  (로그인 상태 유지 체크 필수)")
        return
    if args.login:
        collect([], cfg, login_only=True)
        return

    if args.date:
        days = [args.date]
    elif args.start:
        s = dt.date.fromisoformat(args.start)
        e = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
        days = [(s + dt.timedelta(d)).isoformat() for d in range((e - s).days + 1)]
    else:
        days = [dt.date.today().isoformat()]

    df = collect(days, cfg)
    if df.empty:
        print("수집된 행이 없습니다.")
        return
    if args.dry:
        print(df.to_string(index=False))
        return
    n = upsert_ad(df)
    print(f"data/ad_daily.sqlite 에 {n}행 저장 완료 (수집시각 {df['수집시각'].iloc[0]})")


if __name__ == "__main__":
    _safe_console()
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
