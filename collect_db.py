"""전환(DB) 시트를 읽어 저장한다. 기본 경로는 **서비스 계정**.

    python collect_db.py --check    설정이 제대로 됐는지 단계별 점검 (여기부터 시작)
    python collect_db.py            시트 읽어 data/ad_daily.sqlite 에 저장
    python collect_db.py --dry      저장하지 않고 건수만 확인

서비스 계정은 세션 만료가 없어 스케줄러로 돌리는 무인 자동화에 가장 안정적이다.
필요한 것은 두 가지뿐: Google Sheets API 사용 설정 + 시트를 서비스 계정 이메일에 뷰어 공유.

브라우저 경로(로그인 세션 사용)도 남겨 뒀다:
    python collect_db.py --browser          전용 프로필 (최초 1회 --login 필요)
    python collect_db.py --browser --cdp    켜 둔 내 Chrome 에 붙기
    python collect_db.py --login            전용 프로필에 구글 로그인
"""
from __future__ import annotations

import argparse
import sys

from sources.db_sheet import (browser_login, check_service_account, from_browser,
                              from_service_account, service_account_email)
from store import save_db


def do_check(gid: str) -> int:
    print("서비스 계정 설정 점검\n" + "─" * 46)
    results = check_service_account()
    for ok, msg in results:
        mark = "OK  " if ok else "안됨"
        head, *rest = str(msg).splitlines()
        print(f"[{mark}] {head}")
        for r in rest:
            print(f"        {r}")
    if results and results[-1][0]:
        print("\n설정 완료. `python collect_db.py` 로 저장하면 됩니다.")
        return 0
    email = service_account_email()
    if email:
        print(f"\n시트 공유에 쓸 주소: {email}")
    return 1



def _safe_console() -> None:
    """윈도우 cp949 콘솔에서 인코딩 못 하는 글자가 있어도 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="서비스 계정 설정을 단계별로 점검")
    ap.add_argument("--dry", action="store_true", help="저장하지 않고 요약만 출력")
    ap.add_argument("--gid", default="0", help="시트 탭 gid (브라우저 경로에서만 사용)")
    ap.add_argument("--worksheet", default=None, help="탭 이름 (기본: 첫 번째 탭)")
    ap.add_argument("--browser", action="store_true", help="서비스 계정 대신 브라우저 세션 사용")
    ap.add_argument("--cdp", action="store_true", help="--browser 와 함께: 켜 둔 내 Chrome 에 붙기")
    ap.add_argument("--login", action="store_true", help="브라우저 전용 프로필에 구글 로그인")
    args = ap.parse_args()

    if args.login:
        browser_login()
        return 0
    if args.check:
        return do_check(args.gid)

    if args.browser:
        df = from_browser(gid=args.gid, mode="cdp" if args.cdp else "profile")
    else:
        df = from_service_account(worksheet=args.worksheet)

    if df.empty:
        print("utm_source=kakaopay 인 행이 없습니다.")
        return 0
    print(f"kakaopay 전환 {len(df)}건 · {df['날짜'].min()} ~ {df['날짜'].max()}")
    print(df["구분"].value_counts().to_string())
    if args.dry:
        return 0
    n = save_db(df)
    print(f"data/ad_daily.sqlite 에 {n}건 저장 완료")
    return 0


if __name__ == "__main__":
    _safe_console()
    try:
        sys.exit(main())
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
