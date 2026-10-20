"""카카오모먼트 광고 데이터 - Moment API 에서 직접 가져온다 (메인·서브 두 계정).

메타와 같은 자리에 같은 모양으로 붙이되, API 성격이 달라서 두 가지를 더 신경 쓴다.

1) 리포트는 자기 ID 하나만 받는다
   `/creatives/report` 에 creativeId 를 콤마로 여러 개 주면 HTTP 200 을 주지만
   **첫 번째만 조회하고 나머지는 조용히 버린다.** 묶음 조회가 없으니 소재 하나당
   한 번씩 불러야 한다. 그래서 '어느 소재를 부를지' 를 줄이는 게 전부다.

       /campaigns                      계정당 1회
       /campaigns/report?campaignId=   캠페인당 1회  <- 지출 있는 캠페인만 고르는 데 씀
       /adGroups?campaignId=           고른 캠페인당 1회
       /creatives?adGroupId=           세트당 1회
       /creatives/report?creativeId=   소재당 1회    <- 여기가 대부분

   호출 제한이 아주 빡빡하다. 3.5초 간격으로 불러도 200/429 가 번갈아 나온다 -
   실질 처리량이 7초에 1회쯤이고 Retry-After 헤더도 없다. 게다가 이 제한은 운영 중인
   비즈폼 Zapier 동기화와 같은 토큰을 쓰므로, 호출 수 자체를 줄이는 게 맞다.

   그래서 두 가지로 줄인다.

   a) 캠페인 - 기본은 켜져 있는 것만(`scan_all=False`). 21개를 전부 리포트로 확인하면
      112회 호출 693초가 걸렸다. 꺼진 캠페인의 지난 지출을 봐야 할 때만 True.
   b) 소재 - **세트 지출을 정답으로 삼는다.** 켜진 소재부터 부르고 세트 합계가 다
      설명되면 거기서 멈춘다. 양쪽으로 틀리기 쉬운 자리라 합계로 검증한다.
        · 꺼진 소재까지 전부 부르면: 세트 2개에 소재 81개 × 7초 = 9분
        · 켜진 소재만 보고 끝내면: 조용히 빠진다. 서브 계정 10/19 는 어제 돌다 끈
          소재 때문에 지출의 57%(312,420원)가 사라졌다
      검증을 넣으니 둘 다 피했다. 10/19 기준 서브 44초·메인 51초에 세트 합계와 정확히
      일치한다. 끝까지 안 맞으면(삭제된 소재 등) `attrs["누락"]` 으로 올려 화면에 띄운다.

   화면에서는 어느 범위를 봤는지 캡션에 적어 조용히 빠진 것처럼 보이지 않게 한다.

2) 당일은 기간을 주면 안 된다
   start/end 에 오늘이 하루라도 걸치면 기간 자체를 거부한다(detailCode 60008).
   기간을 아예 빼면 오늘 하루를 돌려준다. 그래서 `report()` 가 '어제까지' 와 '오늘' 을
   나눠 부른다. timeUnit 은 day 또는 all 만 되고 HOUR 는 없다.

3) 전환 시트의 utm_content 는 소재명의 '옛 이름' 일 수 있다
   소재명을 나중에 고치면(`..._메인` 을 붙이는 등) 폼에 박힌 utm_content 는 안 바뀐다.
   정확히 일치로만 붙이면 116건 중 10건만 매칭됐다. 꼬리표를 떼면 116건 전부 붙는다.
   `match_key()` 가 그 규칙이다.

토큰은 만료되지 않는다. 카카오 비즈니스 인증(`kauth.kakao.com/oauth/business/token`) 은
`expires_in` 도 `refresh_token` 도 주지 않는 토큰을 내준다. 실제로 2026-07-24 부터 쓰던
토큰이 3개월 뒤에도 그대로 동작했고, 그걸 쓰던 동기화 스크립트에 갱신 코드가 아예 없었다.
그래서 갱신 장치를 두지 않는다. 다시 발급해야 할 때만 `issue_token.py` 를 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

import pandas as pd

from core.util import today_kst

API = "https://apis.moment.kakao.com/openapi/v4"
HERE = Path(__file__).resolve().parents[1]

# 화면에 쓰는 이름 -> 광고계정 ID. config/Secrets 로 덮을 수 있다.
ACCOUNTS: dict[str, int] = {
    "카카오 메인": 802036,      # 직선기획 주식회사 광고 계정
    "카카오 서브": 928346,      # 법무법인 평온
}

KAKAO_COLUMNS = ["날짜", "캠페인", "광고세트", "소재", "광고ID", "지출", "노출수", "클릭수"]
KAKAO_SOURCES = ("kakao",)

# 호출 간 최소 간격(초). 토큰을 운영 중인 비즈폼 동기화와 공유하므로 넉넉히 둔다.
MIN_INTERVAL = 0.35
_last_call = 0.0


class KakaoError(Exception):
    pass


# ---------------------------------------------------------------- 자격 증명
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def credentials(token: str | None = None) -> str:
    """액세스 토큰. 넘긴 값(Secrets/config.json) > 환경변수 > 레거시 파일 순.

    클라우드는 Secrets 의 `kakao_token`, 내 PC 는 `config.json` 의 `kakao_token` 을
    app 이 읽어 인자로 넘긴다. **두 계정이 모두 보이는 토큰**이어야 한다
    (계정 하나만 보이는 토큰도 있었다 - `issue_token.py --check` 로 확인할 수 있다).

    이 토큰은 만료되지 않는다. 카카오 비즈니스 인증(`oauth/business/token`) 이
    `expires_in` 도 `refresh_token` 도 주지 않고, 실제로 2026-07-24 부터 쓰던 토큰이
    3개월 뒤에도 그대로 동작했다. 그래서 갱신 장치가 필요 없다.

    레거시 경로(kakao_bizform) 는 그 폴더가 은퇴해서 마지막 순서로만 남겨 둔다.
    폴더가 지워져도 config.json/Secrets 만 있으면 돌아간다.
    """
    if token:
        return str(token).strip()
    env = os.environ.get("KAKAO_ACCESS_TOKEN")
    if env:
        return env.strip()
    got = _read_json(HERE / "config.json").get("kakao_token")
    if got:
        return str(got).strip()
    for name in ("token_result.json", "config.json"):      # 레거시
        got = _read_json(HERE.parent / "kakao_bizform" / name).get("access_token")
        if got:
            return str(got).strip()
    raise KakaoError(
        "카카오 액세스 토큰이 없습니다.\n"
        "  · 클라우드: Secrets 에 kakao_token\n"
        "  · 내 PC: config.json 에 \"kakao_token\": \"...\"\n"
        "토큰은 만료되지 않지만, 없거나 권한이 바뀌었으면 "
        "issue_token.py 로 다시 발급할 수 있습니다.")


def accounts(overrides: dict | None = None) -> dict[str, int]:
    """화면 이름 -> 계정 ID. config/Secrets 의 kakao_accounts 로 덮어쓸 수 있다."""
    out = dict(ACCOUNTS)
    for k, v in (overrides or {}).items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- 호출
# 다시 시도해 볼 만한 응답: 호출 제한과 카카오 쪽 일시 장애.
# 503 'upstream connect error' 가 실제로 자주 나는데, 한 번 더 부르면 대개 성공한다.
RETRY_STATUS = (429, 500, 502, 503, 504)


def _get(path: str, account: int, token: str, timeout: int = 40, **params) -> dict:
    """GET 한 번. 호출 제한·일시 장애는 늘려 가며 재시도하고, 401 은 토큰 문제로 번역한다."""
    global _last_call
    import requests

    headers = {"Authorization": f"Bearer {token}", "adAccountId": str(account)}
    last, err = None, None
    for wait in (0, 5, 15, 30):
        if wait:
            time.sleep(wait)
        gap = MIN_INTERVAL - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        try:
            r = requests.get(API + path, headers=headers,
                             params=params or None, timeout=timeout)
        except requests.RequestException as exc:
            _last_call, err = time.monotonic(), exc
            continue                      # 연결 자체가 끊긴 경우도 다시 시도
        _last_call = time.monotonic()
        if r.status_code == 200:
            return r.json()
        last = r
        if r.status_code == 401:
            raise KakaoError(
                "카카오 토큰이 거부됐습니다 (HTTP 401).\n"
                "이 토큰은 만료되지 않으므로, 값이 잘렸거나 광고계정 권한이 바뀐 "
                "경우입니다. issue_token.py 로 다시 발급해 주세요.")
        if r.status_code not in RETRY_STATUS:
            break
    if last is None:
        raise KakaoError(f"카카오 API 에 연결하지 못했습니다 ({path}): {err}")
    raise KakaoError(f"카카오 API 호출 실패 ({path}, HTTP {last.status_code}): {last.text[:200]}")


def _content(body: dict) -> list[dict]:
    return body.get("content") or []


def campaigns(account: int, token: str) -> list[dict]:
    return _content(_get("/campaigns", account, token))


def ad_groups(account: int, token: str, campaign_id: int) -> list[dict]:
    return _content(_get("/adGroups", account, token, campaignId=campaign_id))


def creatives(account: int, token: str, ad_group_id: int) -> list[dict]:
    return _content(_get("/creatives", account, token, adGroupId=ad_group_id))


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


ID_PARAM = {"campaigns": "campaignId", "adGroups": "adGroupId", "creatives": "creativeId"}


def report(kind: str, id_: int, account: int, token: str,
           start: dt.date, end: dt.date) -> list[dict]:
    """일별 리포트. kind 는 'campaigns' / 'adGroups' / 'creatives'.

    돌아오는 행은 날짜마다 하나씩이고, 지출이 없는 날은 dimensions·metrics 가 빈 객체다.

    **당일은 기간을 주면 안 된다.** start/end 에 오늘이 들어가면 기간 자체를 거부한다
    (detailCode 60008 '조회하는 기간의 시작일, 종료일이 유효하지 않습니다'). 범위에 오늘이
    하루라도 걸치면 전부 실패해서, 어제까지와 오늘을 나눠 부른다. 기간을 아예 빼면
    오늘 하루를 돌려준다 - 그게 당일 조회 방법이다.
    """
    key = ID_PARAM[kind]
    today = today_kst()
    base = {"metricsGroup": "BASIC", key: id_}
    rows: list[dict] = []

    past_end = min(end, today - dt.timedelta(days=1))
    if start <= past_end:
        body = _get(f"/{kind}/report", account, token, timeUnit="DAY",
                    start=_ymd(start), end=_ymd(past_end), **base)
        rows += body.get("data") or []

    if start <= today <= end:
        body = _get(f"/{kind}/report", account, token, timeUnit="DAY", **base)
        rows += body.get("data") or []
    return rows


def _spend(rows: list[dict]) -> float:
    return float(sum((r.get("metrics") or {}).get("cost") or 0 for r in rows))


# ---------------------------------------------------------------- 가져오기
def order_creatives(items: list[dict]) -> list[dict]:
    """소재를 '부를 순서' 로 세운다. 켜져 있는 것 먼저.

    지출이 있는 세트라도 그 안의 소재는 대부분 꺼져 있다(메인 계정: 소재 148개 중 ON 18개).
    켜진 것부터 부르면 대개 몇 번 만에 세트 지출이 다 설명돼 거기서 멈출 수 있다.
    """
    켜짐 = [c for c in items if str(c.get("config", "")).upper() == "ON"]
    꺼짐 = [c for c in items if str(c.get("config", "")).upper() != "ON"]
    return 켜짐 + 꺼짐


def fetch(account: int, start: dt.date, end: dt.date, token: str | None = None,
          min_spend: float = 1.0, on_progress=None, scan_all: bool = False) -> pd.DataFrame:
    """기간의 소재별·일별 성과. 지출이 min_spend 미만인 행은 버린다.

    on_progress(단계설명, 지금까지_호출수) 를 주면 화면에 진행 상황을 띄울 수 있다.

    세트 지출을 정답으로 삼아, 켜진 소재부터 부르고 합계가 맞으면 멈춘다. 끝까지 불러도
    안 맞으면(삭제된 소재 등) `df.attrs["누락"]` 에 세트별로 남겨 둔다.

    scan_all: 꺼진 캠페인까지 리포트로 확인할지.
        False (기본) - 켜져 있는(config=ON) 캠페인만 본다. 계정당 캠페인이 21개라
            전부 확인하면 캠페인 리포트만 21번이고, 호출 제한에 걸려 재시도가 겹치면
            10분을 넘긴다. 지금 돌고 있는 광고를 보는 게 대시보드의 일이라 이게 기본이다.
        True - 모든 캠페인의 지출을 리포트로 확인한다. 꺼진 캠페인도 지난 기간엔 지출이
            있을 수 있어서, 지난 날짜를 제대로 보려면 이쪽이 필요하다. 대신 느리다.
    """
    tok = credentials(token)
    calls = 0

    def tick(msg):
        nonlocal calls
        calls += 1
        if on_progress:
            on_progress(msg, calls)

    camps = campaigns(account, tok)
    tick(f"캠페인 {len(camps)}개 확인")

    if not scan_all:
        live = [c for c in camps if str(c.get("config", "")).upper() == "ON"]
    else:
        # 지출이 있는 캠페인만 고른다. config 만 보고 거르지 않고 리포트로 확인한다.
        live = []
        for c in camps:
            rows = report("campaigns", c["id"], account, tok, start, end)
            tick(f"캠페인 지출 확인 {c.get('name', '')[:20]}")
            if _spend(rows) >= min_spend:
                live.append(c)

    out: list[dict] = []
    missing: list[dict] = []
    for c in live:
        groups = ad_groups(account, tok, c["id"])
        tick(f"세트 {len(groups)}개 ({c.get('name', '')[:16]})")
        for g in groups:
            grows = report("adGroups", g["id"], account, tok, start, end)
            tick(f"세트 지출 확인 {g.get('name', '')[:20]}")
            세트지출 = _spend(grows)
            if 세트지출 < min_spend:
                continue                      # 이 세트는 소재를 부를 필요가 없다

            # 세트 지출이 '정답' 이다. 켜진 소재부터 부르고, 세트 합계가 다 설명되면
            # 거기서 멈춘다. 꺼진 소재까지 전부 부르면 세트 2개에 9분이 걸렸다.
            # 반대로 켜진 것만 보고 끝내면 조용히 빠진다 - 서브 계정 10/19 는
            # 어제 돌다 끈 소재 때문에 지출의 57%(312,420원)가 사라졌다.
            설명된지출 = 0.0
            for cr in order_creatives(creatives(account, tok, g["id"])):
                if 설명된지출 >= 세트지출 - min_spend:
                    break                     # 세트 지출이 다 설명됐다
                rows = report("creatives", cr["id"], account, tok, start, end)
                tick(f"소재 {cr.get('name', '')[:24]}")
                for r in rows:
                    m = r.get("metrics") or {}
                    if not m:
                        continue              # 그 날 노출이 없던 소재
                    설명된지출 += float(m.get("cost") or 0)
                    out.append({
                        "날짜": r.get("start"),
                        "캠페인": str(c.get("name", "")).strip(),
                        "광고세트": str(g.get("name", "")).strip(),
                        "소재": str(cr.get("name", "")).strip(),
                        "광고ID": str(cr.get("id", "")),
                        "지출": float(m.get("cost") or 0),
                        "노출수": float(m.get("imp") or 0),
                        "클릭수": float(m.get("click") or 0),
                    })
            남은지출 = 세트지출 - 설명된지출
            if 남은지출 >= min_spend:
                # 소재를 전부 불렀는데도 세트 합계가 안 맞는다 (삭제된 소재 등).
                # 조용히 넘기면 지출이 비므로 호출부가 알 수 있게 모아 둔다.
                missing.append({"광고세트": str(g.get("name", "")).strip(),
                                "세트지출": 세트지출, "소재합계": 설명된지출,
                                "누락": 남은지출})
            tick(f"세트 완료 {g.get('name', '')[:20]}")

    df = normalize(out, min_spend=min_spend)
    df.attrs["누락"] = missing
    return df


def normalize(rows: list[dict], min_spend: float = 1.0) -> pd.DataFrame:
    """fetch 가 모은 행 → 표준 스키마 (메타와 같은 열)."""
    if not rows:
        return pd.DataFrame(columns=KAKAO_COLUMNS)
    df = pd.DataFrame(rows)
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    for c in ("지출", "노출수", "클릭수"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = df[df["날짜"].notna() & df["소재"].ne("")]
    if min_spend is not None:
        df = df[df["지출"] >= min_spend]
    return df[KAKAO_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------- 전환 매칭
def match_key(name) -> str:
    """소재명 매칭용 정규화.

    소재명을 나중에 고쳐도 폼에 박힌 utm_content 는 그대로 남는다. 실제로 붙은 꼬리표가
    `_메인` / ` / 메인` 형태라, 그것만 떼면 전환이 제자리를 찾는다.

        '법무법인 평온 - kakao8_1029x258_메인'    -> '법무법인 평온 - kakao8_1029x258'
        '법무법인 평온 - kakao13_1029x258 / 메인'  -> '법무법인 평온 - kakao13_1029x258'

    `/ 간편양식` 처럼 utm_content 에도 그대로 들어 있는 꼬리표는 떼지 않는다.
    떼면 서로 다른 소재가 한 키로 합쳐져 전환이 엉뚱한 곳에 붙는다.
    """
    import re

    s = str(name or "").strip().lower()
    s = re.sub(r"\s*/\s*(메인|서브)\s*$", "", s)
    s = re.sub(r"[_\-\s]+(메인|서브)\s*$", "", s)
    return s.strip()


def creative_index(account: int, token: str | None = None) -> set[str]:
    """그 계정의 **모든** 소재명 매칭 키. 꺼진 캠페인·꺼진 소재까지 전부.

    지출 조회(`fetch`) 와 달리 리포트를 부르지 않고 목록만 훑으므로 싸다.
    서브 계정(캠페인 2개·소재 39종) 은 6회면 끝난다. 구조는 자주 바뀌지 않아
    길게 캐시해 두고 쓴다.

    쓰는 곳: 전환을 어느 계정 것으로 볼지 가르는 데 쓴다. 전환 시트에는 메인·서브가
    모두 utm_source=kakao 로 들어와서, 이 목록이 없으면 메인 화면에 서브 전환이
    '미매칭' 으로 쏟아진다(10/14~20 기준 63건). 반대도 마찬가지(335건).
    """
    tok = credentials(token)
    out: set[str] = set()
    for c in campaigns(account, tok):
        for g in ad_groups(account, tok, c["id"]):
            for cr in creatives(account, tok, g["id"]):
                out.add(match_key(cr.get("name")))
    return out


def split_conversions(conv: pd.DataFrame, account: int, primary: int,
                      indexes: dict[int, set[str]]) -> pd.DataFrame:
    """카카오 전환을 계정별로 나눈다.

    두 계정이 전환 시트에서 구분되지 않으니, 소재명 목록으로 가른다.

        지정 계정(primary) 이 아닌 계정  -> 자기 소재명 목록에 '있는' 것
        지정 계정(primary)              -> 다른 계정 목록에 '없는' 나머지 전부

    지정 계정을 나머지로 두는 이유는 호출 수다. 메인 계정은 캠페인이 21개라 소재명
    목록을 만드는 데도 수십 번 호출이 필요하다. 작은 계정만 목록을 만들고 큰 계정은
    여집합으로 두면, 두 계정이 전환을 빠짐없이 한 번씩 나눠 갖는다.

    어느 계정 소재도 아닌 전환(삭제된 소재 등) 은 지정 계정 쪽에 남아 '미매칭' 으로
    보인다. 조용히 사라지는 것보다 눈에 띄는 쪽이 낫다.
    """
    if conv is None or conv.empty:
        return conv
    others: set[str] = set()
    for acct, names in indexes.items():
        if acct != primary:
            others |= names
    if account == primary:
        return conv[~conv["key"].isin(others)].reset_index(drop=True)
    mine = indexes.get(account) or set()
    return conv[conv["key"].isin(mine)].reset_index(drop=True)


def conversions(db: pd.DataFrame) -> pd.DataFrame:
    """전환 시트에서 카카오 건만 골라 매칭 키를 붙인다.

    메인·서브 두 계정이 전환 시트에서는 모두 utm_source=kakao 다. 계정을 나누는 건
    소재명뿐이라, 계정별 화면에서는 그 계정 소재명과 붙는 것만 집계된다.
    """
    if db is None or db.empty:
        return db
    src = db["utm_source"].astype(str).str.strip().str.lower()
    out = db[src.isin(KAKAO_SOURCES)].copy()
    out["key"] = out["utm_content"].map(match_key)
    return out.reset_index(drop=True)


def key_collisions(names) -> dict[str, list[str]]:
    """꼬리표를 뗀 뒤 서로 다른 소재명이 한 키로 합쳐지는 경우.

    합쳐지면 전환이 엉뚱한 소재에 붙을 수 있으므로 화면에 경고로 띄운다.
    """
    seen: dict[str, set] = {}
    for n in names:
        seen.setdefault(match_key(n), set()).add(str(n).strip())
    return {k: sorted(v) for k, v in seen.items() if len(v) > 1}
