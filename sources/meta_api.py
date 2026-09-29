"""메타(페이스북) 광고 데이터 - Marketing API 에서 직접 가져온다.

카카오페이는 광고센터에 API 가 없어 브라우저로 긁어 구글 시트에 쌓지만, 메타는 API 가
있으므로 시트를 거치지 않고 대시보드가 곧바로 호출한다. 시스템 사용자 토큰이라 만료가 없다.

    campaign_name  캠페인      -> 전환 시트의 utm_medium
    adset_name     광고세트    -> 전환 시트의 utm_campaign
    ad_name        소재        -> 전환 시트의 utm_content   (매칭 기준)
    ad_id          광고 ID     -> 전환 시트의 utm_term      (일부 행만 있어 보조로만)

짧은 기간은 동기 호출로 충분하다. 라이프타임처럼 긴 구간은 비동기 리포트 잡이 필요한데,
대시보드는 최근 며칠만 보므로 여기서는 쓰지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd

API = "https://graph.facebook.com/v21.0"
HERE = Path(__file__).resolve().parents[1]

META_COLUMNS = ["날짜", "캠페인", "광고세트", "소재", "광고ID", "지출", "노출수", "클릭수"]


class MetaError(Exception):
    pass


# ---------------------------------------------------------------- 자격 증명
def _from_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def credentials(token: str | None = None, account: str | None = None) -> tuple[str, str]:
    """토큰·계정 ID. 넘긴 값 > 환경변수 > .env 파일 순.

    클라우드에서는 Streamlit Secrets 로 넘겨 주고(app 이 읽어서 인자로 전달),
    내 PC 에서는 모노레포 루트 `.env` 의 META_SYSTEM_USER_TOKEN / META_AD_ACCOUNT_ID 를 쓴다.
    """
    env = {**_from_env_file(HERE.parent / ".env"), **_from_env_file(HERE / ".env"), **os.environ}
    token = token or env.get("META_SYSTEM_USER_TOKEN")
    account = account or env.get("META_AD_ACCOUNT_ID")
    if not token or not account:
        raise MetaError(
            "메타 토큰·계정 ID 가 없습니다.\n"
            "  · 클라우드: Secrets 에 meta_token / meta_account_id\n"
            "  · 내 PC: .env 의 META_SYSTEM_USER_TOKEN / META_AD_ACCOUNT_ID")
    if not str(account).startswith("act_"):
        account = f"act_{account}"
    return token, account


# ---------------------------------------------------------------- 가져오기
def fetch(start: dt.date, end: dt.date, token: str | None = None, account: str | None = None,
          min_spend: float = 1.0, timeout: int = 90) -> pd.DataFrame:
    """기간의 소재별·일별 성과. 지출이 min_spend 미만인 행은 버린다.

    '지출이 있는 것만' 이 기본이다. 계정에 리턴찬스 상품이 여러 개 섞여 있어서,
    그날 실제로 돈이 나간 캠페인만 봐야 화면이 쓸모 있다.
    """
    import requests

    tok, acct = credentials(token, account)
    params = {
        "access_token": tok,
        "level": "ad",
        "time_increment": 1,
        "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
        "fields": "date_start,campaign_name,adset_name,ad_name,ad_id,spend,impressions,clicks",
        "limit": 500,
    }
    rows, url, first = [], f"{API}/{acct}/insights", True
    while url:
        r = requests.get(url, params=params if first else None, timeout=timeout)
        if r.status_code != 200:
            raise MetaError(f"메타 API 호출 실패 (HTTP {r.status_code}): {r.text[:200]}")
        body = r.json()
        rows += body.get("data", [])
        url, first = (body.get("paging") or {}).get("next"), False
    return normalize(rows, min_spend=min_spend)


def normalize(rows: list[dict], min_spend: float = 1.0) -> pd.DataFrame:
    """API 응답 → 표준 스키마."""
    if not rows:
        return pd.DataFrame(columns=META_COLUMNS)
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "날짜": pd.to_datetime(df.get("date_start"), errors="coerce").dt.date,
        "캠페인": df.get("campaign_name", "").astype(str).str.strip(),
        "광고세트": df.get("adset_name", "").astype(str).str.strip(),
        "소재": df.get("ad_name", "").astype(str).str.strip(),
        "광고ID": df.get("ad_id", "").astype(str).str.strip(),
        "지출": pd.to_numeric(df.get("spend"), errors="coerce").fillna(0.0),
        "노출수": pd.to_numeric(df.get("impressions"), errors="coerce").fillna(0.0),
        "클릭수": pd.to_numeric(df.get("clicks"), errors="coerce").fillna(0.0),
    })
    out = out[out["날짜"].notna() & out["소재"].ne("")]
    if min_spend is not None:
        out = out[out["지출"] >= min_spend]
    return out[META_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------- 전환 매칭
META_SOURCES = ("meta", "facebook", "ig", "instagram", "threads")


def match_key(name) -> str:
    """소재명 매칭용 정규화. 앞뒤 공백·대소문자 차이만 흡수한다."""
    return str(name or "").strip().lower()


def conversions(db: pd.DataFrame) -> pd.DataFrame:
    """전환 시트에서 메타 건만 골라 매칭 키를 붙인다.

    db 는 sources.db_sheet 가 돌려주는 표준 스키마여야 한다. 단 db_sheet 는 kakaopay 만
    남기므로, 메타를 쓰려면 all_sources=True 로 읽은 표를 넘겨야 한다.
    """
    if db is None or db.empty:
        return db
    src = db["utm_source"].astype(str).str.strip().str.lower()
    out = db[src.isin(META_SOURCES)].copy()
    out["key"] = out["utm_content"].map(match_key)
    return out.reset_index(drop=True)
