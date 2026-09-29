"""구글 시트 접속을 한 번만 하고 재사용한다.

측정해 보면 시트 한 번 읽는 3초 중 1.9초가 '여는 데' 쓰인다.

    Credentials      0.04초
    open_by_key      1.44초   <- 매번 다시 열고 있었다
    get_worksheet    0.44초   <- 매번 다시 찾고 있었다
    get_all_values   1.08초   <- 실제 데이터

읽을 때마다 새로 열면 탭 하나당 1.9초가 붙는다. 카카오페이는 광고 시트 2탭 + 전환 시트로
세 번 열어 6초를 낭비했다. 클라이언트와 worksheet 핸들을 캐시하면 데이터 시간만 남는다.

핸들 캐시는 탭이 새로 만들어지거나 삭제되면 낡은 것을 들고 있을 수 있다. 그래서 호출부는
`with_retry` 로 감싸 실패하면 캐시를 비우고 한 번 더 시도한다.
"""
from __future__ import annotations

import gspread

_CLIENTS: dict = {}
_WORKSHEETS: dict = {}


def _creds_key(creds_info: dict | None, creds_file: str, scopes) -> tuple:
    who = (creds_info or {}).get("client_email") if creds_info else creds_file
    return (who, tuple(scopes))


def client(creds_info: dict | None, creds_file: str, scopes: list[str]):
    """gspread 클라이언트. 같은 자격 증명이면 한 번만 만든다."""
    from pathlib import Path

    from google.oauth2.service_account import Credentials

    key = _creds_key(creds_info, creds_file, scopes)
    if key in _CLIENTS:
        return _CLIENTS[key]
    if creds_info:
        creds = Credentials.from_service_account_info(dict(creds_info), scopes=scopes)
    else:
        p = Path(creds_file)
        if not p.exists():
            raise FileNotFoundError(creds_file)
        creds = Credentials.from_service_account_file(str(p), scopes=scopes)
    _CLIENTS[key] = gspread.authorize(creds)
    return _CLIENTS[key]


def spreadsheet(sheet_id: str, creds_info, creds_file, scopes):
    """스프레드시트 핸들. open_by_key 가 1.4초라 캐시한다."""
    key = (sheet_id, _creds_key(creds_info, creds_file, scopes))
    if key not in _WORKSHEETS:
        _WORKSHEETS[key] = client(creds_info, creds_file, scopes).open_by_key(sheet_id)
    return _WORKSHEETS[key]


def worksheet(sheet_id: str, title: str | None, creds_info, creds_file, scopes):
    """탭 핸들. title 이 None 이면 첫 번째 탭."""
    key = (sheet_id, title, _creds_key(creds_info, creds_file, scopes))
    if key not in _WORKSHEETS:
        sh = spreadsheet(sheet_id, creds_info, creds_file, scopes)
        _WORKSHEETS[key] = sh.worksheet(title) if title else sh.get_worksheet(0)
    return _WORKSHEETS[key]


def invalidate() -> None:
    """핸들 캐시를 비운다. 탭을 새로 만들거나 읽기가 실패했을 때."""
    _WORKSHEETS.clear()


def with_retry(fn, *args, **kwargs):
    """한 번 실패하면 캐시를 비우고 다시 시도한다.

    탭이 삭제·재생성되면 캐시된 핸들이 낡는다. 그때 조용히 틀린 값을 주는 게 아니라
    404 로 실패하므로, 캐시를 버리고 새로 열어 한 번 더 해 본다.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        invalidate()
        return fn(*args, **kwargs)
