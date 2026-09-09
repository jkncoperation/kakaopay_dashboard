"""공용 파싱 헬퍼 - 숫자/날짜/헤더 정규화."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

__all__ = ["to_num", "to_ratio", "norm_header", "parse_any_date", "dates_from_filename",
           "today_kst", "now_kst", "KST"]

_STRIP = ("원", "₩", ",", " ", " ", "회", "건", "명")


def to_num(v, default: float = 0.0) -> float:
    """'4,200원' → 4200.0, '12,649' → 12649.0, '-' → 0.0, None → 0.0"""
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip()
    for ch in _STRIP:
        s = s.replace(ch, "")
    s = s.replace("%", "")
    if s in ("", "-", "–", "N/A", "NA"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def to_ratio(v) -> float:
    """클릭률을 0~1 비율로 통일.

    광고센터 다운로드 파일은 이미 비율(0.0010965)로 주고,
    화면/캡처에서는 '0.11%' 처럼 퍼센트로 보인다. 둘 다 받아 비율로 맞춘다.
    """
    if v is None:
        return 0.0
    is_pct = isinstance(v, str) and "%" in v
    n = to_num(v)
    if is_pct or n > 1.0:          # 1.0 초과면 퍼센트 표기로 본다 (CTR 100% 초과는 없음)
        return n / 100.0
    return n


def norm_header(s) -> str:
    """헤더 비교용: 괄호주석·공백·기호 제거 후 소문자."""
    s = re.sub(r"\(.*?\)", "", str(s or ""))
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", s).lower()


_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_any_date(v):
    """'26.09.09 11:33:36' / 'Sep 9, 2026 11:41 AM' / '2026-09-09 ...' / '2026/9/9' → date"""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()

    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))

    m = re.match(r"^(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", s)      # 26.09.09
    if m:
        return _safe_date(2000 + int(m[1]), int(m[2]), int(m[3]))

    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)   # Sep 9, 2026
    if m and m[1].lower() in _MONTHS:
        return _safe_date(int(m[3]), _MONTHS.index(m[1].lower()) + 1, int(m[2]))

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)                    # 20260909
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    return None


def _safe_date(y: int, m: int, d: int):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def dates_from_filename(name: str):
    """'..._소재_20260909_20260909.xlsx' → (date(2026,9,9), date(2026,9,9))

    한 개만 있으면 (그 날짜, 그 날짜), 못 찾으면 (None, None).
    """
    if not name:
        return None, None
    found = re.findall(r"(20\d{6})", str(name))
    if not found:
        return None, None
    start = parse_any_date(found[0])
    end = parse_any_date(found[1]) if len(found) > 1 else start
    if start and end and end < start:
        start, end = end, start
    return start, end


# ---------------------------------------------------------------- 오늘 날짜
# 광고센터도 전환 시트도 한국 시간으로 하루를 끊는다. 그런데 Streamlit Cloud 는 UTC 라
# date.today() 를 그대로 쓰면 한국 새벽~오전 9시 사이에 '오늘' 이 하루 뒤처진다.
KST = ZoneInfo("Asia/Seoul")


def today_kst() -> dt.date:
    """한국 기준 오늘 날짜. 서버 시간대와 무관하게 같은 답을 준다."""
    return dt.datetime.now(KST).date()


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)
