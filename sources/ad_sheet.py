"""광고 데이터를 구글 시트에 쓰고 읽는다 - 클라우드 배포용 다리.

카카오 로그인 때문에 수집기는 내 PC 에서만 돌 수 있다. 그래서 수집 결과를 구글 시트에
올려 두고, Streamlit Cloud 의 대시보드는 그 시트를 읽는다.

    내 PC: collect_all.py -> data/ad_daily.sqlite -> (이 모듈) -> 구글 시트
    클라우드: app.py -> (이 모듈) -> 구글 시트

탭을 둘로 나눈다.
    KakaopayToday  당일 실시간. 30분마다 통째로 갈아끼운다. 계속 변하는 값.
    KakaopayDaily  마감된 과거 날짜. 날짜 단위로만 채우고 기존 날짜는 건드리지 않는다.

시트 열은 광고센터 다운로드 파일과 같은 이름·순서를 쓴다(`일자`, `소진비용`, `클릭률` …).
사람이 다운로드 파일을 그대로 붙여 넣어도 읽히도록 하기 위해서다. 그래서 읽을 때는
'1,234' 같은 콤마 숫자와 '0.22%' 같은 퍼센트 표기를 모두 받아 준다.

시트에 쓰려면 서비스 계정이 그 시트의 **편집자**여야 한다(읽기만 할 때는 뷰어면 충분).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from core.util import to_num, to_ratio
from parsers.adcenter_file import AD_COLUMNS
from sources.db_sheet import DBSheetError, SA_FILE, service_account_email, service_account_path

WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TODAY_WORKSHEET = "KakaopayToday"
CLOSED_WORKSHEET = "KakaopayDaily"

# 마감 탭 = 광고센터 다운로드 파일과 같은 열 구성 (날짜 열 이름만 '일자')
CLOSED_COLUMNS = ["일자", "소재", "ON/OFF", "상태", "광고그룹", "광고상품",
                  "소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC"]
# 당일 탭 = 위에 더해 언제·어디서 수집했는지까지 (대시보드가 '수집 시각' 을 보여 준다)
TODAY_COLUMNS = CLOSED_COLUMNS + ["시작일", "종료일", "출처", "수집시각"]

DATE_HEADERS = ("일자", "날짜")
NUM_HEADERS = ("소진비용", "노출수", "클릭수", "도달수", "eCPM", "CPC")


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
    import time
    sh.add_worksheet(worksheet, rows=2000, cols=max(len(TODAY_COLUMNS), 26))
    for _ in range(5):
        time.sleep(1.0)
        try:
            return sh.worksheet(worksheet)
        except Exception:
            continue
    raise DBSheetError(f"'{worksheet}' 탭을 만들었지만 열지 못했습니다. 다시 실행해 주세요.")


# ---------------------------------------------------------------- 형식 변환
def _to_sheet(df: pd.DataFrame, columns: list[str]) -> list[list]:
    """표준 스키마 -> 시트에 쓸 2차원 값. 숫자는 숫자로 넣어 시트 서식이 살아 있게 한다."""
    d = df.copy() if df is not None else pd.DataFrame(columns=AD_COLUMNS)
    for c in AD_COLUMNS:
        if c not in d:
            d[c] = "" if c not in NUM_HEADERS and c != "클릭률" else 0.0
    d = d.rename(columns={"날짜": "일자"})
    d["일자"] = d["일자"].map(
        lambda v: v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else ("" if v is None else str(v)))
    d = d[columns].sort_values(["일자", "광고그룹", "소재"])
    return [columns] + d.astype(object).where(pd.notna(d), "").values.tolist()


def _from_sheet(vals: list[list]) -> pd.DataFrame:
    """시트의 2차원 값 -> 표준 스키마.

    헤더 이름으로 찾으므로 열 순서가 달라도 되고, 없는 열은 빈값/0 으로 채운다.
    '1,234' 콤마 숫자와 '0.22%' 퍼센트 표기를 모두 받는다.
    """
    if not vals or len(vals) < 2:
        return pd.DataFrame(columns=AD_COLUMNS)
    header = [str(h).strip() for h in vals[0]]
    width = len(header)
    rows = [(r + [""] * width)[:width] for r in vals[1:]]
    raw = pd.DataFrame(rows, columns=header)

    out = pd.DataFrame(index=raw.index)
    date_col = next((h for h in DATE_HEADERS if h in raw.columns), None)
    out["날짜"] = pd.to_datetime(raw[date_col], errors="coerce").dt.date if date_col else None
    for c in ["소재", "ON/OFF", "상태", "광고그룹", "광고상품", "시작일", "종료일", "출처", "수집시각"]:
        out[c] = raw[c].astype(str).str.strip() if c in raw.columns else ""
    for c in NUM_HEADERS:
        out[c] = raw[c].map(to_num) if c in raw.columns else 0.0
    out["클릭률"] = raw["클릭률"].map(to_ratio) if "클릭률" in raw.columns else 0.0

    out = out[out["날짜"].notna() & out["소재"].astype(str).str.strip().ne("")]
    return out[AD_COLUMNS].reset_index(drop=True)


def _write(ws, df: pd.DataFrame, columns: list[str]) -> int:
    values = _to_sheet(df, columns)
    ws.clear()
    ws.update(values, "A1", value_input_option="USER_ENTERED")
    return len(values) - 1


def _read(ws) -> pd.DataFrame:
    return _from_sheet(ws.get_all_values())


# ---------------------------------------------------------------- 쓰기 / 읽기
def push_split(df: pd.DataFrame, sheet_id: str, today=None,
               today_ws: str = TODAY_WORKSHEET, closed_ws: str = CLOSED_WORKSHEET,
               creds_info: dict | None = None, creds_file: str = SA_FILE,
               include_past: bool = False) -> dict:
    """당일 탭을 갈아끼운다. include_past=True 일 때만 마감 탭도 손댄다.

    마감 탭은 사람이 다운로드 파일 기준으로 직접 관리하는 곳이라, 30분마다 도는 수집기가
    멋대로 덮어쓰면 안 된다. 그래서 기본값은 '당일 탭만'. 과거 날짜를 로컬에서 올리고
    싶을 때만 include_past=True 로 명시한다(그때도 해당 날짜의 행만 갈아끼운다).
    """
    today = today or dt.date.today()
    d = df.copy() if df is not None else pd.DataFrame(columns=AD_COLUMNS)
    if len(d):
        d["날짜"] = pd.to_datetime(d["날짜"], errors="coerce").dt.date
        d = d[d["날짜"].notna()]

    cur = d[d["날짜"] == today] if len(d) else d
    past = d[d["날짜"] < today] if len(d) else d

    ws_today = _open(sheet_id, today_ws, creds_info, creds_file, WRITE_SCOPES, create=True)
    n_today = _write(ws_today, cur, TODAY_COLUMNS)

    if not include_past:
        return {"today": n_today, "closed": None, "closed_dates": []}

    ws_closed = _open(sheet_id, closed_ws, creds_info, creds_file, WRITE_SCOPES, create=True)
    existing = _read(ws_closed)
    if len(past):
        keep = existing[~existing["날짜"].isin(set(past["날짜"]))] if len(existing) else existing
        merged = pd.concat([keep, past], ignore_index=True)
    else:
        merged = existing
    if len(merged):
        merged = merged[merged["날짜"] != today]       # 오늘은 마감 탭에 두지 않는다
    n_closed = _write(ws_closed, merged, CLOSED_COLUMNS)
    return {"today": n_today, "closed": n_closed,
            "closed_dates": sorted({str(x) for x in merged["날짜"]}) if len(merged) else []}


def read_split(sheet_id: str, today_ws: str = TODAY_WORKSHEET, closed_ws: str = CLOSED_WORKSHEET,
               creds_info: dict | None = None, creds_file: str = SA_FILE,
               start=None, end=None) -> pd.DataFrame:
    """두 탭을 합쳐 읽는다. 같은 날짜가 겹치면 실시간 탭을 우선한다."""
    frames = []
    for name in (today_ws, closed_ws):          # 당일 탭을 먼저 넣어 우선권을 준다
        try:
            frames.append(_read(_open(sheet_id, name, creds_info, creds_file, WRITE_SCOPES)))
        except Exception:
            continue                            # 탭이 아직 없으면 건너뛴다
    if not frames:
        return pd.DataFrame(columns=AD_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["날짜", "광고그룹", "소재"], keep="first")
    if start is not None:
        df = df[df["날짜"] >= start]
    if end is not None:
        df = df[df["날짜"] <= end]
    return df.reset_index(drop=True)


def last_collected_at(sheet_id: str, today_ws: str = TODAY_WORKSHEET,
                      creds_info: dict | None = None, creds_file: str = SA_FILE) -> str | None:
    """당일 탭의 마지막 수집 시각."""
    try:
        df = _read(_open(sheet_id, today_ws, creds_info, creds_file, WRITE_SCOPES))
    except Exception:
        return None
    vals = [v for v in df["수집시각"].astype(str) if v.strip()] if len(df) else []
    return max(vals) if vals else None
