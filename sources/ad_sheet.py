"""광고 데이터를 구글 시트에 쓰고 읽는다 - 클라우드 배포용 다리.

카카오 로그인 때문에 수집기는 내 PC 에서만 돌 수 있다. 그래서 수집 결과를 구글 시트에
올려 두고, Streamlit Cloud 의 대시보드는 그 시트를 읽는다.

    내 PC: collect_all.py -> data/ad_daily.sqlite -> (이 모듈) -> 구글 시트
    클라우드: app.py -> (이 모듈) -> 구글 시트

시트에 쓰려면 서비스 계정이 그 시트의 **편집자**여야 한다(읽기만 할 때는 뷰어면 충분).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from parsers.adcenter_file import AD_COLUMNS
from sources.db_sheet import DBSheetError, SA_FILE, service_account_email, service_account_path

WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_WORKSHEET = "KakaopayRAW"          # 예전 단일 탭 (마이그레이션·폴백용)

# 탭을 둘로 나눈다.
#   Today  - 당일 실시간. 30분마다 통째로 갈아끼운다. 계속 변하는 값.
#   Daily  - 마감된 과거 날짜. 날짜 단위로만 채워 넣고 기존 날짜는 건드리지 않는다.
# 한 탭에 전부 담고 매번 전체 재작성하면, 로컬 저장소가 비었을 때 과거 기록까지 날아간다.
TODAY_WORKSHEET = "KakaopayToday"
CLOSED_WORKSHEET = "KakaopayDaily"


def _client(creds_info: dict | None, creds_file: str, scopes: list[str]):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise DBSheetError(f"gspread/google-auth 가 설치돼 있지 않습니다: {exc}")
    if creds_info:
        creds = Credentials.from_service_account_info(dict(creds_info), scopes=scopes)
    else:
        p = service_account_path(creds_file)
        if not p.exists():
            raise DBSheetError(f"'{creds_file}' 이 없습니다.")
        creds = Credentials.from_service_account_file(str(p), scopes=scopes)
    return gspread.authorize(creds)


def _open(sheet_id: str, worksheet: str, creds_info, creds_file, scopes, create: bool = False):
    gc = _client(creds_info, creds_file, scopes)
    try:
        sh = gc.open_by_key(sheet_id)
    except Exception as exc:
        who = service_account_email(creds_info, creds_file) or "서비스 계정"
        raise DBSheetError(
            f"광고 시트를 열지 못했습니다. 시트 ID 가 맞는지, 그리고 아래 주소가 "
            f"그 시트에 공유돼 있는지 확인해 주세요.\n  시트 ID: {sheet_id}\n  주소: {who}\n"
            f"  (원인: {str(exc).splitlines()[0]})") from exc
    try:
        return sh.worksheet(worksheet)
    except Exception:
        if not create:
            raise DBSheetError(
                f"시트에 '{worksheet}' 탭이 없습니다. 수집기를 한 번 돌리면 자동으로 만들어집니다.")
    # 새 탭을 만든 직후에는 구글 쪽 메타데이터가 아직 안 잡혀 바로 쓰면 실패할 때가 있다.
    # 만든 뒤 이름으로 다시 집어와 확실히 준비된 객체를 쓴다.
    import time
    sh.add_worksheet(worksheet, rows=2000, cols=max(len(AD_COLUMNS), 26))
    for _ in range(5):
        time.sleep(1.0)
        try:
            return sh.worksheet(worksheet)
        except Exception:
            continue
    raise DBSheetError(f"'{worksheet}' 탭을 만들었지만 열지 못했습니다. 다시 실행해 주세요.")


def push_ad(df: pd.DataFrame, sheet_id: str, worksheet: str = DEFAULT_WORKSHEET,
            creds_info: dict | None = None, creds_file: str = SA_FILE) -> int:
    """로컬에 쌓인 광고 데이터 전체를 시트에 통째로 올린다(기존 내용 대체).

    부분 갱신 대신 전체 교체를 쓰는 이유: 로컬 sqlite 가 항상 정본이고,
    부분 갱신은 실패 시 시트와 로컬이 어긋난 채 남을 수 있어서다.
    """
    ws = _open(sheet_id, worksheet, creds_info, creds_file, WRITE_SCOPES, create=True)
    d = df.copy() if df is not None else pd.DataFrame(columns=AD_COLUMNS)
    for c in AD_COLUMNS:
        if c not in d:
            d[c] = ""
    d = d[AD_COLUMNS]
    d["날짜"] = d["날짜"].map(
        lambda v: v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else ("" if v is None else str(v)))
    d = d.sort_values(["날짜", "광고그룹", "소재"])
    values = [AD_COLUMNS] + d.astype(object).where(pd.notna(d), "").values.tolist()
    ws.clear()
    ws.update(values, "A1", value_input_option="USER_ENTERED")
    return len(d)


def read_ad(sheet_id: str, worksheet: str = DEFAULT_WORKSHEET,
            creds_info: dict | None = None, creds_file: str = SA_FILE,
            start=None, end=None) -> pd.DataFrame:
    """시트에서 광고 데이터를 읽어 표준 스키마로 돌려준다 (클라우드 대시보드용)."""
    ws = _open(sheet_id, worksheet, creds_info, creds_file, WRITE_SCOPES)
    vals = ws.get_all_values()
    if not vals or len(vals) < 2:
        return pd.DataFrame(columns=AD_COLUMNS)
    df = pd.DataFrame(vals[1:], columns=[h.strip() for h in vals[0]])
    for c in AD_COLUMNS:
        if c not in df:
            df[c] = ""
    df = df[AD_COLUMNS]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    for c in ["소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = df[df["날짜"].notna()]
    if start is not None:
        df = df[df["날짜"] >= start]
    if end is not None:
        df = df[df["날짜"] <= end]
    return df.reset_index(drop=True)


def last_collected_at(sheet_id: str, worksheet: str = DEFAULT_WORKSHEET,
                      creds_info: dict | None = None, creds_file: str = SA_FILE,
                      date=None) -> str | None:
    df = read_ad(sheet_id, worksheet, creds_info, creds_file)
    if df.empty:
        return None
    if date is not None:
        df = df[df["날짜"] == date]
    return max(df["수집시각"].astype(str)) if len(df) else None


# ---------------------------------------------------------------- 마감/실시간 분리
def _write(ws, df: pd.DataFrame) -> int:
    d = df.copy() if df is not None else pd.DataFrame(columns=AD_COLUMNS)
    for c in AD_COLUMNS:
        if c not in d:
            d[c] = ""
    d = d[AD_COLUMNS]
    d["날짜"] = d["날짜"].map(
        lambda v: v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else ("" if v is None else str(v)))
    d = d.sort_values(["날짜", "광고그룹", "소재"])
    values = [AD_COLUMNS] + d.astype(object).where(pd.notna(d), "").values.tolist()
    ws.clear()
    ws.update(values, "A1", value_input_option="USER_ENTERED")
    return len(d)


def _read(ws) -> pd.DataFrame:
    vals = ws.get_all_values()
    if not vals or len(vals) < 2:
        return pd.DataFrame(columns=AD_COLUMNS)
    df = pd.DataFrame(vals[1:], columns=[h.strip() for h in vals[0]])
    for c in AD_COLUMNS:
        if c not in df:
            df[c] = ""
    df = df[AD_COLUMNS]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    for c in ["소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df[df["날짜"].notna()].reset_index(drop=True)


def push_split(df: pd.DataFrame, sheet_id: str, today=None,
               today_ws: str = TODAY_WORKSHEET, closed_ws: str = CLOSED_WORKSHEET,
               creds_info: dict | None = None, creds_file: str = SA_FILE) -> dict:
    """당일은 실시간 탭에 통째로, 마감된 날짜는 마감 탭에 날짜 단위로 채워 넣는다.

    마감 탭은 '해당 날짜의 행만' 갈아끼우므로, 로컬에 없는 예전 날짜는 시트에 그대로 남는다.
    """
    today = today or dt.date.today()
    d = df.copy() if df is not None else pd.DataFrame(columns=AD_COLUMNS)
    if len(d):
        d["날짜"] = pd.to_datetime(d["날짜"], errors="coerce").dt.date
        d = d[d["날짜"].notna()]

    cur = d[d["날짜"] == today] if len(d) else d
    past = d[d["날짜"] < today] if len(d) else d

    ws_today = _open(sheet_id, today_ws, creds_info, creds_file, WRITE_SCOPES, create=True)
    n_today = _write(ws_today, cur)

    ws_closed = _open(sheet_id, closed_ws, creds_info, creds_file, WRITE_SCOPES, create=True)
    existing = _read(ws_closed)
    if len(past):
        dates = set(past["날짜"])
        keep = existing[~existing["날짜"].isin(dates)] if len(existing) else existing
        merged = pd.concat([keep, past], ignore_index=True)
    else:
        merged = existing
    merged = merged[merged["날짜"] != today] if len(merged) else merged   # 오늘은 마감 탭에 두지 않는다
    n_closed = _write(ws_closed, merged)
    return {"today": n_today, "closed": n_closed,
            "closed_dates": sorted({str(x) for x in merged["날짜"]}) if len(merged) else []}


def read_split(sheet_id: str, today_ws: str = TODAY_WORKSHEET, closed_ws: str = CLOSED_WORKSHEET,
               creds_info: dict | None = None, creds_file: str = SA_FILE,
               start=None, end=None) -> pd.DataFrame:
    """두 탭을 합쳐 읽는다. 같은 날짜가 겹치면 실시간 탭을 우선한다."""
    frames = []
    for name, first in ((today_ws, True), (closed_ws, False)):
        try:
            frames.append((_read(_open(sheet_id, name, creds_info, creds_file, WRITE_SCOPES)), first))
        except Exception:
            continue                      # 탭이 아직 없으면 건너뛴다
    if not frames:
        return pd.DataFrame(columns=AD_COLUMNS)
    df = pd.concat([f for f, _ in sorted(frames, key=lambda x: not x[1])], ignore_index=True)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["날짜", "광고그룹", "소재"], keep="first")
    if start is not None:
        df = df[df["날짜"] >= start]
    if end is not None:
        df = df[df["날짜"] <= end]
    return df.reset_index(drop=True)
