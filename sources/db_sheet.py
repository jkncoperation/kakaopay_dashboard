"""전환(DB) 시트 읽기 - 서비스 계정.

시트가 비공개(개인정보가 들어 있어 공개하면 안 된다)라 서비스 계정으로만 읽는다.
필요한 것: Google Sheets API 사용 설정 + 시트를 서비스 계정 이메일에 뷰어 공유.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pandas as pd

from core.util import parse_any_date

DEFAULT_SHEET_ID = "1BTfbVKKCbe-6g2x3SQilnFAILMXB-Yj0C4GLG77o1r4"
DEFAULT_GID = "0"

# 1-C. 상태 분류 정규식 - 기존 보고서 시트와 동일
# 비교 전에 공백을 모두 지우므로 '미팅 확정' 도 '미팅확정' 으로 걸린다.
# 부분 일치라 '미팅확정v2', '미팅확정v3' 처럼 뒤에 뭐가 붙어도 잡힌다.
RX_NOGO = re.compile(r"접수불가|채무미비|자산과다|소득미비|소득증빙불가|면책5년미만|DTI100%미만|채무조정중|장기연체")
RX_RECV = re.compile(r"접수완료|담보접수|법인파산접수|미팅보류|협의중|관리")
RX_MEET = re.compile(r"미팅확정")
RX_APPR = re.compile(r"승인예정|승인완료")

BUCKETS = ["진행불가", "접수", "미팅", "승인"]
# 시트 원본 값은 '접수상태'/'승인상태' 로 담는다. 집계 플래그가 '접수'/'승인' 이라
# 같은 이름을 쓰면 열이 중복돼 조용히 덮어써진다.
DB_COLUMNS = ["날짜", "utm_source", "utm_campaign", "utm_content",
              "접수상태", "승인상태", "구분", *BUCKETS]


class DBSheetError(Exception):
    pass


def status_flags(접수: str, 승인: str = "") -> dict[str, int]:
    """구분별로 해당하는지 (0/1). 한 건이 여러 구분에 들어갈 수 있다.

    깔때기라서 뒷단계는 앞단계를 포함한다 - 미팅확정된 건은 접수도 된 것으로 센다.
    그래서 접수 >= 미팅 이 된다. 승인은 '승인' 열, 나머지는 '접수' 열에서 본다.
    """
    a = str(접수 or "").replace(" ", "")
    b = str(승인 or "").replace(" ", "")
    meet = bool(RX_MEET.search(a))
    return {"진행불가": int(bool(RX_NOGO.search(a))),
            "접수": int(bool(RX_RECV.search(a)) or meet),
            "미팅": int(meet),
            "승인": int(bool(RX_APPR.search(b)))}


def status_bucket(접수: str, 승인: str = "") -> str:
    """그 건을 한마디로 부르면 무엇인가 (목록에 보여 줄 이름).

    집계는 status_flags 로 하고, 이건 표시용 라벨이다. 뒷단계가 우선.
    """
    f = status_flags(접수, 승인)
    for name in ("승인", "미팅", "접수", "진행불가"):
        if f[name]:
            return name
    return "미분류"


SA_FILE = "service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def service_account_path(creds_file: str = SA_FILE) -> Path:
    return Path(__file__).resolve().parents[1] / creds_file


def service_account_email(creds_info: dict | None = None,
                          creds_file: str = SA_FILE) -> str | None:
    """서비스 계정 이메일 - 이 주소를 시트에 뷰어(광고 시트는 편집자)로 공유해야 한다."""
    if creds_info:
        return creds_info.get("client_email")
    p = service_account_path(creds_file)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("client_email")
    except Exception:
        return None


def _credentials(creds_info: dict | None, creds_file: str):
    from google.oauth2.service_account import Credentials
    if creds_info:
        return Credentials.from_service_account_info(dict(creds_info), scopes=SCOPES)
    p = service_account_path(creds_file)
    if not p.exists():
        raise DBSheetError(
            f"'{creds_file}' 이 없습니다. 구글 클라우드에서 받은 서비스 계정 키(JSON)를 "
            f"'{p.parent.name}' 폴더에 이 이름으로 넣어 주세요.")
    return Credentials.from_service_account_file(str(p), scopes=SCOPES)


def _explain(exc: Exception, email: str | None, sheet_id: str) -> DBSheetError:
    """구글이 돌려준 오류를 사람이 바로 고칠 수 있는 말로 바꾼다."""
    msg = str(exc)
    who = email or "서비스 계정"
    if "has not been used in project" in msg or "SERVICE_DISABLED" in msg or "accessNotConfigured" in msg:
        return DBSheetError(
            "이 프로젝트에서 Google Sheets API 가 아직 켜져 있지 않습니다.\n"
            "구글 클라우드 콘솔 > API 및 서비스 > 라이브러리 에서 'Google Sheets API' 를 '사용' 하세요.")
    if "PERMISSION_DENIED" in msg or "403" in msg or isinstance(exc, PermissionError):
        return DBSheetError(
            f"시트 접근 권한이 없습니다.\n시트 공유에서 아래 주소를 추가해 주세요:\n  {who}")
    if "not found" in msg.lower() or "404" in msg:
        return DBSheetError(
            f"시트를 찾지 못했습니다. 공유가 안 됐거나 시트 ID 가 다릅니다.\n"
            f"  · 시트 ID: {sheet_id}\n  · 공유할 주소: {who}")
    return DBSheetError(f"시트를 읽지 못했습니다: {msg}")


def from_service_account(sheet_id: str = DEFAULT_SHEET_ID, worksheet: str | None = None,
                         creds_info: dict | None = None,
                         creds_file: str = SA_FILE) -> pd.DataFrame:
    """서비스 계정으로 읽기 - 세션 만료가 없어 무인 자동화에 가장 안정적.

    필요한 것: Google Sheets API 사용 설정 + 시트를 서비스 계정 이메일에 뷰어 공유.
    (Drive API 는 필요 없다 - open_by_key 는 sheets.googleapis.com 만 쓴다)
    """
    try:
        import gspread
    except ImportError as exc:
        raise DBSheetError(f"gspread/google-auth 가 설치돼 있지 않습니다: {exc}")

    email = service_account_email(creds_info, creds_file)
    creds = _credentials(creds_info, creds_file)
    try:
        sh = gspread.authorize(creds).open_by_key(sheet_id)
        ws = sh.worksheet(worksheet) if worksheet else sh.get_worksheet(0)
        vals = ws.get_all_values()
    except DBSheetError:
        raise
    except Exception as exc:
        raise _explain(exc, email, sheet_id) from exc
    if not vals:
        raise DBSheetError("시트가 비어 있습니다.")
    return _normalize(pd.DataFrame(vals[1:], columns=[h.strip() for h in vals[0]]))


def check_service_account(sheet_id: str = DEFAULT_SHEET_ID,
                          creds_file: str = SA_FILE) -> list[tuple[bool, str]]:
    """설정을 단계별로 점검해 (성공여부, 설명) 목록을 돌려준다."""
    out: list[tuple[bool, str]] = []
    p = service_account_path(creds_file)
    if not p.exists():
        out.append((False, f"키 파일 없음 - '{p}' 에 service_account.json 을 넣어 주세요"))
        return out
    out.append((True, f"키 파일 있음 ({p.name})"))

    email = service_account_email(creds_file=creds_file)
    if not email:
        out.append((False, "키 파일에서 client_email 을 읽지 못했습니다 - 서비스 계정 키가 맞는지 확인하세요"))
        return out
    out.append((True, f"서비스 계정: {email}"))

    try:
        df = from_service_account(sheet_id, creds_file=creds_file)
    except DBSheetError as exc:
        out.append((False, str(exc)))
        return out
    out.append((True, f"시트 읽기 성공 - kakaopay 전환 {len(df)}건"))
    return out


# ------------------------------------------------------------------ 정규화
def _pick(cols: list[str], *names: str) -> str | None:
    low = {str(c).strip().lower(): c for c in cols}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """시트 원본 → 표준 DB 스키마. utm_source=kakaopay 행만 남긴다."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=DB_COLUMNS)
    raw = raw.rename(columns=lambda c: str(c).strip())
    cols = list(raw.columns)

    c_src = _pick(cols, "utm_source")
    c_time = _pick(cols, "신청 시간", "신청시간", "신청일시")
    c_content = _pick(cols, "utm_content")
    if not (c_src and c_content):
        raise DBSheetError(
            f"DB 시트가 아닌 것 같습니다. utm_source/utm_content 열이 없습니다. 읽은 헤더: {cols[:16]}")

    out = pd.DataFrame(index=raw.index)
    out["utm_source"] = raw[c_src].astype(str).str.strip()
    out["utm_campaign"] = raw[_pick(cols, "utm_campaign")].astype(str).str.strip() \
        if _pick(cols, "utm_campaign") else ""
    out["utm_content"] = raw[c_content].astype(str).str.strip()
    out["접수상태"] = raw[_pick(cols, "접수")].astype(str) if _pick(cols, "접수") else ""
    out["승인상태"] = raw[_pick(cols, "승인")].astype(str) if _pick(cols, "승인") else ""
    out["날짜"] = raw[c_time].map(parse_any_date) if c_time else None

    out = out[out["utm_source"].str.lower() == "kakaopay"].copy()
    pairs = list(zip(out["접수상태"], out["승인상태"]))
    out["구분"] = [status_bucket(a, b) for a, b in pairs]
    flags = [status_flags(a, b) for a, b in pairs]
    for b in BUCKETS:
        out[b] = [f[b] for f in flags]
    return out[DB_COLUMNS].reset_index(drop=True)
