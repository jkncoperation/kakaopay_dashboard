"""광고 + 전환을 한 번에 수집한다. 스케줄러와 대시보드 새로고침 버튼이 같이 쓴다.

    python collect_all.py              오늘 것 수집
    python collect_all.py --days 3     오늘 포함 최근 3일 (광고만; 전환은 시트 전체가 항상 최신)
    python collect_all.py --date 2026-09-08

한쪽이 실패해도 다른 쪽은 계속 진행하고, 마지막에 결과를 요약한다.
스케줄러가 남긴 로그를 사람이 읽을 수 있도록 한 줄 요약을 마지막에 찍는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from collect_live import collect, load_config
from collect_db import do_check  # noqa: F401  (진단은 collect_db 쪽과 공유)
from sources.db_sheet import from_service_account
from store import load_ad, save_db, upsert_ad


def _safe_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def collect_ads(days: list[str]) -> tuple[bool, str]:
    try:
        df = collect(days)
        if df.empty:
            return False, "광고: 수집된 행이 없습니다"
        n = upsert_ad(df)
        return True, (f"광고: {n}행 저장 (소진 {df['소진비용'].sum():,.0f}원, "
                      f"{df['수집시각'].iloc[0]})")
    except Exception as exc:
        return False, f"광고: 실패 - {exc}"


def collect_conversions() -> tuple[bool, str]:
    try:
        df = from_service_account()
        n = save_db(df)
        return True, f"전환: {n}건 저장"
    except Exception as exc:
        return False, f"전환: 실패 - {str(exc).splitlines()[0]}"


def push_to_sheet(include_past: bool = False) -> tuple[bool, str]:
    """클라우드 대시보드가 읽을 수 있도록 광고 데이터를 구글 시트로 올린다.

    기본은 **당일 탭만** 갱신한다. 마감 탭은 사람이 다운로드 파일 기준으로 직접 관리하는
    곳이라 30분마다 도는 수집기가 덮어쓰면 안 된다.
    과거 날짜까지 올리려면 `python collect_all.py --push-past`.

    config.json 의 ad_sheet_id 가 비어 있으면(로컬 전용) 아무것도 하지 않는다.
    """
    cfg = load_config()
    sheet_id = (cfg.get("ad_sheet_id") or "").strip()
    if not sheet_id:
        return True, "시트 업로드: 안 함 (config.json 의 ad_sheet_id 가 비어 있음)"
    try:
        from sources.ad_sheet import push_split
        r = push_split(load_ad(), sheet_id,
                       today_ws=cfg.get("ad_worksheet_today", "KakaopayToday"),
                       closed_ws=cfg.get("ad_worksheet_closed", "KakaopayDaily"),
                       include_past=include_past)
        if r["closed"] is None:
            return True, f"시트 업로드: 당일 {r['today']}행 (마감 탭은 그대로 둠)"
        return True, (f"시트 업로드: 당일 {r['today']}행 / 마감 {r['closed']}행"
                      f" ({len(r['closed_dates'])}일치)")
    except Exception as exc:
        return False, f"시트 업로드: 실패 - {str(exc).splitlines()[0]}"


def run(days: list[str], push_past: bool = False) -> int:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"===== {stamp} 수집 시작 ({', '.join(days)}) =====")
    ok_ad, msg_ad = collect_ads(days)
    print(" ", msg_ad)
    ok_db, msg_db = collect_conversions()
    print(" ", msg_db)
    ok_up, msg_up = push_to_sheet(include_past=push_past)
    print(" ", msg_up)
    oks = [ok_ad, ok_db, ok_up]
    both = "성공" if all(oks) else ("부분 성공" if any(oks) else "실패")
    print(f"[{stamp}] {both} | {msg_ad} | {msg_db} | {msg_up}")
    return 0 if all(oks) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="하루만 (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=1, help="오늘 포함 최근 N일 (기본 1)")
    ap.add_argument("--push-past", action="store_true",
                    help="로컬의 과거 날짜도 마감 탭에 올린다 (평소에는 마감 탭을 건드리지 않음)")
    args = ap.parse_args()

    if args.date:
        days = [args.date]
    else:
        today = dt.date.today()
        days = [(today - dt.timedelta(d)).isoformat() for d in range(args.days - 1, -1, -1)]
    return run(days, push_past=args.push_past)


if __name__ == "__main__":
    _safe_console()
    try:
        sys.exit(main())
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
