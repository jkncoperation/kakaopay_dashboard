"""당일 실시간 광고 데이터 수집기 - 광고센터 화면 → 광고 시트의 당일 탭.

대시보드는 광고 시트의 `KakaopayToday` 탭을 읽는다. 지금까지는 사람이 광고센터 다운로드 파일을
올려야 채워졌는데, 이 수집기를 띄워 두면 10분마다 알아서 채운다.

    python collect_live.py --login     # 최초 1회 카카오 로그인 (.env 가 있으면 자동으로 시도)
    python collect_live.py --once      # 한 번만 수집해서 시트에 기록
    python collect_live.py --dry       # 수집해서 보여 주기만 (시트에 안 씀)
    python collect_live.py             # 10분마다 반복 (Ctrl+C 로 종료)

당일 탭만 갈아끼운다. 마감 탭(`KakaopayDaily`)은 사람이 다운로드 파일로 관리하는 곳이라
수집기가 건드리지 않는다(ad_sheet.push_split 의 기본값). **파일 업로드 기능은 그대로 쓸 수 있고**,
올린 값은 다음 수집 때 실시간 값으로 다시 덮인다.

필요: pip install -r requirements.txt ; playwright install chromium
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from core.util import now_kst, today_kst          # noqa: E402
from sources import adcenter_live as live         # noqa: E402
from sources.ad_sheet import push_split           # noqa: E402

CONFIG_PATH = HERE / "config.json"
DEFAULT_AD_SHEET = "1HNxQePggP1zPSoUDaNwdwPXKej_bD6A3N5rhWM1Q0r4"


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg.setdefault("ad_sheet_id", DEFAULT_AD_SHEET)
    return cfg


def claim_single_instance(name: str = "kakaopay_collect_live") -> bool:
    """이 수집기가 이미 돌고 있으면 False. (윈도우 이름 있는 뮤텍스)

    두 개가 같이 돌면 같은 크롬 프로필을 동시에 열어 브라우저가 죽는다.
    작업 스케줄러 등록 후 손으로 한 번 더 실행하는 일이 흔해서 막아 둔다.
    """
    if sys.platform != "win32":
        return True
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{name}")
    if not handle:
        return True                                   # 뮤텍스를 못 만들면 그냥 진행
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return False
    globals()["_INSTANCE_MUTEX"] = handle             # 프로세스가 살아 있는 동안 유지
    return True


def seconds_to_next_slot(interval_min: int, now: dt.datetime | None = None) -> float:
    """다음 정각 슬롯까지 남은 초. 10분이면 :00 :10 :20 :30 :40 :50 에 맞춘다.

    시작한 시각부터 10분씩 재면 3시 03분·13분·23분처럼 어중간해진다. 시계에 맞춰 두면
    언제 켜도 갱신 시각이 같아서, 대시보드를 보는 사람이 "지금 값이 몇 분 기준인지" 알기 쉽다.
    """
    now = now or now_kst()
    step = max(1, int(interval_min))
    elapsed = now.minute % step                      # 슬롯 시작점에서 지난 분
    slot = (now.replace(second=0, microsecond=0)
            - dt.timedelta(minutes=elapsed)
            + dt.timedelta(minutes=step))
    return max(1.0, (slot - now).total_seconds())


def collect_once(page, c: dict, cfg: dict, write: bool = True, verbose: bool = True) -> int:
    """한 번 수집해서 당일 탭에 기록. 기록한 행 수를 돌려준다."""
    day = today_kst()
    df, picked = live.collect_today(page, c, day, verbose=verbose)
    if df.empty:
        print(f"  [{now_kst():%H:%M}] 오늘 지출 있는 광고그룹이 없어 건너뜁니다")
        return 0
    spend = df["소진비용"].map(lambda v: float(str(v).replace(",", "") or 0)).sum()
    print(f"  [{now_kst():%H:%M}] 소재 {len(df)}행 · 광고그룹 {len(picked)}개 · 소진 합계 {spend:,.0f}원"
          + ("" if write else "  (미기록 - --dry)"))
    if not write:
        print(df.head(10).to_string(index=False, max_colwidth=20))
        return len(df)
    res = push_split(df, cfg["ad_sheet_id"], today=day,
                     today_ws=cfg.get("ad_worksheet_today", "KakaopayToday"),
                     closed_ws=cfg.get("ad_worksheet_closed", "KakaopayDaily"))
    print(f"  [{now_kst():%H:%M}] 당일 탭 갱신 완료 ({res['today']}행)")
    return res["today"]


def cycle(page, ctx, c: dict, cfg: dict, write: bool = True, verbose: bool = True) -> bool:
    """한 사이클. 세션이 끊겼으면 자동 로그인하고 곧바로 다시 수집한다.
    돌아온 값이 False 면 로그인을 되살리지 못한 것이라 루프를 멈춰야 한다."""
    try:
        collect_once(page, c, cfg, write=write, verbose=verbose)
        live.save_session(ctx, c)
        return True
    except live.CollectError as exc:
        if "LOGIN_REQUIRED" not in str(exc):
            print("  ! 수집 실패:", exc)
            return True                               # 화면 문제면 다음 사이클에 다시 해 본다
    print("  ! 세션이 만료됐습니다. 자동 로그인을 시도합니다.")
    kid, kpw = live.load_env_credentials()
    if not (kid and kpw and live.auto_login(page, c, kid, kpw)):
        print("  자동 로그인 실패. `python collect_live.py --login` 으로 다시 로그인해 주세요.")
        return False
    live.save_session(ctx, c)
    print("  자동 로그인 성공 — 이어서 수집합니다.")
    try:
        collect_once(page, c, cfg, write=write, verbose=verbose)
        live.save_session(ctx, c)
    except Exception as exc:
        print("  ! 재로그인 후 수집 실패(다음 사이클에 다시 시도):", exc)
    return True


def login(pw, c: dict) -> bool:
    """.env 계정으로 자동 로그인 → 막히면 창을 띄워 직접 로그인 → 저장됐는지 확인."""
    kid, kpw = live.load_env_credentials()
    done = False
    ctx, page = live.open_context(pw, c, headed=True)
    try:
        if kid and kpw:
            print(".env 계정으로 자동 로그인 시도…")
            done = live.auto_login(page, c, kid, kpw)
            print("자동 로그인 성공." if done else
                  "자동 로그인이 안 됐습니다(추가 인증 필요). 창에서 직접 로그인해 주세요.")
        else:
            page.goto(live.group_list_url(c, today_kst()), wait_until="domcontentloaded", timeout=60_000)
        if not done:
            time.sleep(2)
            if live.check_stay_signed_in(page):
                print("'로그인 상태 유지' 를 켜 뒀습니다 (세션이 오래 갑니다).")
            print("창에서 카카오 로그인을 해 주세요. 로그인되면 자동으로 감지해 창을 닫습니다.")
            deadline = time.time() + c["relogin_wait_min"] * 60
            while time.time() < deadline:
                time.sleep(3)
                if page.is_closed():
                    break
                try:
                    # 로그인만 끝나면 어느 화면이든(내 광고계정·캠페인·광고그룹) 인정한다.
                    # 목록 표가 뜨는 화면까지 가 있기를 기다리면 사람이 다른 탭에 머물 때 못 잡는다.
                    if "adcenter.kakaopay.com" in page.url and "accounts.kakao" not in page.url:
                        print("로그인 확인됐습니다.")
                        break
                except Exception:
                    break
        live.save_session(ctx, c)
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    # 창만 닫고 로그인은 안 한 경우를 바로 알 수 있게, 저장된 프로필로 다시 들어가 확인한다
    print("로그인 상태가 저장됐는지 확인하는 중…")
    ctx, page = live.open_context(pw, c, headed=False)
    try:
        groups = live.read_groups(page, c, today_kst())
        print(f"로그인 확인 완료 — 광고그룹 {len(groups)}개가 읽힙니다.")
        live.save_session(ctx, c)
        return True
    except live.CollectError:
        print("!! 로그인이 저장되지 않았습니다. 광고그룹 목록이 보이는 것까지 확인한 뒤 창을 닫아 주세요.")
        return False
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="카카오 로그인만 수행")
    ap.add_argument("--once", action="store_true", help="한 번만 수집하고 종료")
    ap.add_argument("--dry", action="store_true", help="수집만 하고 시트에 쓰지 않음")
    ap.add_argument("--quiet", action="store_true", help="진행 내용을 덜 출력")
    args = ap.parse_args()

    cfg = load_config()
    c = live.collector_config(cfg)
    if not (args.once or args.login) and not claim_single_instance():
        print("이미 수집기가 돌고 있습니다. (작업 스케줄러로 등록돼 있는지 확인해 보세요)")
        sys.exit(0)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if args.login:
            sys.exit(0 if login(pw, c) else 1)

        ctx, page = live.open_context(pw, c)
        try:
            while True:
                try:
                    if not cycle(page, ctx, c, cfg, write=not args.dry, verbose=not args.quiet):
                        return                        # 로그인을 되살리지 못했다
                except Exception as exc:              # 네트워크 등 일시 오류 → 다음 사이클
                    print("  ! 오류:", exc, file=sys.stderr)
                if args.once:
                    return
                if c.get("align_to_clock", True):
                    wait = seconds_to_next_slot(c["interval_min"])
                    print(f"  다음 갱신 {(now_kst() + dt.timedelta(seconds=wait)):%H:%M} "
                          f"({wait / 60:.1f}분 뒤)")
                else:
                    wait = c["interval_min"] * 60
                time.sleep(wait)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
