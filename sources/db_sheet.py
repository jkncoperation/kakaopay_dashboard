"""전환(DB) 시트 읽기 - 세 가지 경로를 우선순위대로.

1) 공개 CSV  : 시트가 '링크가 있는 모든 사용자 보기'면 설정 없이 바로 읽힘
2) 파일 업로드: 시트를 xlsx/csv 로 내려받아 넣기 (현재 기본 경로)
3) 서비스 계정: gspread 자동화 (마지막 수단)

`probe_public_csv()` 로 1)이 되는지 먼저 판별할 수 있다.
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
RX_NOGO = re.compile(r"접수불가|채무미비|자산과다|소득미비|소득증빙불가|면책5년미만|DTI100%미만|채무조정중")
RX_RECV = re.compile(r"접수완료|담보접수|법인파산접수|미팅보류|협의중|관리")
RX_MEET = re.compile(r"미팅확정")
RX_APPR = re.compile(r"승인예정|승인완료")

DB_COLUMNS = ["날짜", "utm_source", "utm_campaign", "utm_content", "접수", "승인", "구분"]


class DBSheetError(Exception):
    pass


def status_bucket(접수: str, 승인: str = "") -> str:
    """진행불가 / 접수 / 미팅 / 승인 / 미분류.

    승인은 '승인' 열에서, 나머지는 '접수' 열에서 본다(지시서 1-C).
    """
    a = str(접수 or "").replace(" ", "")
    b = str(승인 or "").replace(" ", "")
    if RX_APPR.search(b):
        return "승인"
    if RX_MEET.search(a):
        return "미팅"
    if RX_RECV.search(a):
        return "접수"
    if RX_NOGO.search(a):
        return "진행불가"
    return "미분류"


def public_csv_url(sheet_id: str = DEFAULT_SHEET_ID, gid: str = DEFAULT_GID) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def probe_public_csv(sheet_id: str = DEFAULT_SHEET_ID, gid: str = DEFAULT_GID,
                     timeout: int = 10) -> tuple[bool, str]:
    """공개 CSV 로 읽히는지 확인. (가능여부, 설명)"""
    try:
        import requests
    except ImportError:
        return False, "requests 패키지가 없습니다."
    try:
        r = requests.get(public_csv_url(sheet_id, gid), timeout=timeout, allow_redirects=True)
    except Exception as exc:
        return False, f"연결 실패: {exc}"
    if r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""):
        return True, "공개 시트로 바로 읽을 수 있습니다."
    if r.status_code in (401, 403):
        return False, ("시트가 비공개입니다(HTTP %d). 시트를 내려받아 업로드하거나, "
                       "서비스 계정을 설정해야 합니다." % r.status_code)
    return False, f"예상치 못한 응답(HTTP {r.status_code})."


# ------------------------------------------------------------------ 읽기
def from_public_csv(sheet_id: str = DEFAULT_SHEET_ID, gid: str = DEFAULT_GID) -> pd.DataFrame:
    import requests
    r = requests.get(public_csv_url(sheet_id, gid), timeout=20, allow_redirects=True)
    if r.status_code != 200:
        raise DBSheetError(f"공개 CSV 읽기 실패 (HTTP {r.status_code}).")
    return _normalize(pd.read_csv(io.BytesIO(r.content), dtype=object))


def from_file(src, filename: str | None = None) -> pd.DataFrame:
    """내려받은 시트 파일(xlsx/csv)에서 읽기."""
    name = filename or (str(src) if isinstance(src, (str, Path)) else "")
    ext = Path(name).suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        raw = pd.read_excel(src, dtype=object)
    else:
        data = Path(src).read_bytes() if isinstance(src, (str, Path)) else src.read()
        raw = None
        for enc in ("utf-8-sig", "cp949", "utf-8"):
            try:
                raw = pd.read_csv(io.BytesIO(data), dtype=object, encoding=enc)
                break
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        if raw is None:
            raise DBSheetError("CSV 인코딩을 알 수 없습니다 (utf-8/cp949 모두 실패).")
    return _normalize(raw)


SA_FILE = "service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def service_account_path(creds_file: str = SA_FILE) -> Path:
    return Path(__file__).resolve().parents[1] / creds_file


def service_account_email(creds_info: dict | None = None,
                          creds_file: str = SA_FILE) -> str | None:
    """서비스 계정 이메일 - 이 주소를 시트에 뷰어로 공유해야 한다."""
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
            "구글 클라우드 콘솔 > API 및 서비스 > 라이브러리 에서 'Google Sheets API' 를 '사용' 하세요.\n"
            "(켠 직후 1~2분 뒤에 반영됩니다)")
    if "PERMISSION_DENIED" in msg or "403" in msg or isinstance(exc, PermissionError):
        return DBSheetError(
            f"시트 접근 권한이 없습니다.\n시트 공유 버튼에서 아래 주소를 **뷰어**로 추가해 주세요:\n  {who}")
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
    out["접수"] = raw[_pick(cols, "접수")].astype(str) if _pick(cols, "접수") else ""
    out["승인"] = raw[_pick(cols, "승인")].astype(str) if _pick(cols, "승인") else ""
    out["날짜"] = raw[c_time].map(parse_any_date) if c_time else None

    out = out[out["utm_source"].str.lower() == "kakaopay"].copy()
    out["구분"] = [status_bucket(a, b) for a, b in zip(out["접수"], out["승인"])]
    return out[DB_COLUMNS].reset_index(drop=True)


def from_browser(sheet_id: str = DEFAULT_SHEET_ID, gid: str = DEFAULT_GID,
                 mode: str = "profile", profile_dir: str = "google-profile",
                 cdp_url: str = "http://127.0.0.1:9222", timeout: int = 60_000) -> pd.DataFrame:
    """로그인된 브라우저로 시트를 CSV 로 받아 읽는다.

    mode="profile" : 전용 프로필. 최초 1회 `python collect_db.py --login` 필요
    mode="cdp"     : 이미 켜 둔 내 Chrome 에 붙기. 로그인 불필요
    """
    from core.browser import browser_context

    root = Path(__file__).resolve().parents[1]
    url = public_csv_url(sheet_id, gid)
    with browser_context(mode=mode, profile_dir=root / profile_dir,
                         headless=True, cdp_url=cdp_url) as ctx:
        resp = ctx.request.get(url, timeout=timeout)
        status, body, final_url = resp.status, resp.body(), resp.url

    hint = ("`python collect_db.py --login` 으로 시트가 보이는 구글 계정에 로그인해 주세요."
            if mode == "profile" else
            "붙은 Chrome 이 시트가 보이는 구글 계정으로 로그인돼 있는지 확인해 주세요.")
    if status != 200 or "accounts.google.com" in final_url:
        raise DBSheetError(
            f"구글에 로그인돼 있지 않거나 이 계정에 시트 권한이 없습니다.\n{hint}")
    head = body[:200].lstrip()
    if head.startswith(b"<") or b"<html" in head.lower():
        raise DBSheetError(f"CSV 대신 로그인 페이지가 왔습니다.\n{hint}")
    return _normalize(pd.read_csv(io.BytesIO(body), dtype=object))


def browser_login(profile_dir: str = "google-profile", sheet_id: str = DEFAULT_SHEET_ID) -> None:
    """창을 띄워 구글 로그인만 수행. 시트가 보이면 창을 닫으면 된다."""
    from core.browser import browser_context, first_page

    root = Path(__file__).resolve().parents[1]
    with browser_context(mode="profile", profile_dir=root / profile_dir,
                         headless=False, viewport={"width": 1300, "height": 900}) as ctx:
        page = first_page(ctx)
        page.goto(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
        print("브라우저에서 구글 로그인을 마치고, 시트 내용이 보이면 창을 닫아 주세요.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
