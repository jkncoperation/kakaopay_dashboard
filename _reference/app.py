"""
카카오페이 광고 × DB 통합 대시보드 (Streamlit)
------------------------------------------------
데이터 소스
  1) KakaopayRAW  : kakaopay_scraper.py 가 기록하는 소재별 일별 성과 (구글 시트)
  2) DB RAW       : 랜딩페이지 DB 시트 (utm_source = kakaopay 행만 사용)
매칭 규칙
  - DB utm_content  kakaopay_ad{세트번호}-{소재번호}
  - 광고 소재명     {접두어}{세트번호}_ad{소재번호}   (예: 채무조정_ad6 = 세트1, 채무조정2_ad6 = 세트2)
  - 세트별로 별도 집계, 같은 번호라도 세트가 다르면 다른 소재
  - DB 단가 = 소진 ÷ DB, DB 0건은 "-"
  - CPC 수정 전 지출 차감: 소재별 소진과 합계에서 모두 차감

실행:  streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import re

import altair as alt
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="카카오페이 광고 × DB 대시보드", page_icon="📊", layout="wide")

# ---------------------------------------------------------------- 설정
def secret(key: str, default=None):
    try:
        return st.secrets[key]
    except Exception:            # secrets.toml 이 없거나 키가 없을 때
        return default


DATA_SHEET_ID = secret("data_sheet_id", "1qcffn2CLiEK4U13vr_dR95-kZq99xI9vkPY_hAJM6m4")
DATA_WS = secret("data_worksheet", "KakaopayRAW")
DB_SHEET_ID = secret("db_sheet_id", "1BTfbVKKCbe-6g2x3SQilnFAILMXB-Yj0C4GLG77o1r4")
DB_WS = secret("db_worksheet", None)          # None → 첫 번째 탭
APP_PASSWORD = secret("app_password", "")

# 상태 분류 (기존 보고서 시트와 동일한 정규식)
RX_NOGO = re.compile(r"접수불가|채무미비|자산과다|소득미비|소득증빙불가|면책5년미만|DTI100%미만|채무조정중")
RX_RECV = re.compile(r"접수완료|담보접수|법인파산접수|미팅보류|협의중|관리")
RX_MEET = re.compile(r"미팅확정")
RX_APPR = re.compile(r"승인예정|승인완료")

COLOR_SPEND, COLOR_DB = "#2a78d6", "#eb6834"      # 검증된 기본 팔레트 슬롯 1·2


# ---------------------------------------------------------------- 인증
def gate() -> None:
    if not APP_PASSWORD:
        return
    if st.session_state.get("auth"):
        return
    pw = st.text_input("비밀번호", type="password")
    if pw == APP_PASSWORD:
        st.session_state["auth"] = True
        st.rerun()
    st.stop()


@st.cache_resource
def gclient():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return gspread.authorize(creds)


@st.cache_data(ttl=600, show_spinner="시트 불러오는 중…")
def load_sheet(sheet_id: str, ws_name: str | None) -> pd.DataFrame:
    sh = gclient().open_by_key(sheet_id)
    ws = sh.worksheet(ws_name) if ws_name else sh.get_worksheet(0)
    vals = ws.get_all_values()
    if not vals:
        return pd.DataFrame()
    header = [h.strip() for h in vals[0]]
    df = pd.DataFrame(vals[1:], columns=header)
    return df.loc[:, [c for c in df.columns if c]]


# ---------------------------------------------------------------- 파싱
def to_num(s) -> float:
    if s is None:
        return 0.0
    s = str(s).replace(",", "").replace("원", "").replace("₩", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_db_date(s: str):
    """'26.09.09 11:33:36' / 'Sep 9, 2026 11:41 AM' / '2026-09-09' → date"""
    s = (s or "").strip()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})", s)
    if m:
        return dt.date(2000 + int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^([A-Z][a-z]{2}) (\d+), (\d{4})", s)
    if m:
        mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].index(m[1]) + 1
        return dt.date(int(m[3]), mo, int(m[2]))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None


RX_CREATIVE = re.compile(r"^(.*?)(\d*)_ad(\d+)$")      # 채무조정3_ad2 → ('채무조정','3','2')
RX_CONTENT = re.compile(r"kakaopay_ad(\d+)-(\d+)", re.I)  # kakaopay_ad3-2 → ('3','2')


def creative_key(name: str):
    m = RX_CREATIVE.match((name or "").strip())
    if not m:
        return None
    setno = int(m[2]) if m[2] else 1
    return setno, int(m[3])


def content_key(content: str):
    m = RX_CONTENT.search(content or "")
    return (int(m[1]), int(m[2])) if m else None


def status_bucket(recv: str) -> str:
    s = (recv or "").replace(" ", "")
    if RX_APPR.search(s):
        return "승인"
    if RX_MEET.search(s):
        return "미팅"
    if RX_RECV.search(s):
        return "접수"
    if RX_NOGO.search(s):
        return "진행불가"
    return "미분류"


# ---------------------------------------------------------------- 메인
def main() -> None:
    gate()
    st.title("카카오페이 광고 × DB 통합 대시보드")

    ad = load_sheet(DATA_SHEET_ID, DATA_WS)
    db = load_sheet(DB_SHEET_ID, DB_WS)
    if ad.empty:
        st.error("KakaopayRAW 시트가 비어 있습니다. kakaopay_scraper.py 를 먼저 실행해 주세요.")
        st.stop()

    # ---- 광고 데이터 정리
    ad = ad.rename(columns={"소진 비용": "소진비용"})
    ad["날짜"] = pd.to_datetime(ad["날짜"], errors="coerce").dt.date
    for c in ["소진비용", "노출수", "클릭수", "도달수"]:
        if c in ad:
            ad[c] = ad[c].map(to_num)
    ad = ad.dropna(subset=["날짜"])
    ad["key"] = ad["소재"].map(creative_key)

    # ---- DB 데이터 정리
    db.columns = [c.strip() for c in db.columns]
    db = db[db["utm_source"].astype(str).str.strip().str.lower() == "kakaopay"].copy()
    db["date"] = db["신청 시간"].map(parse_db_date)
    db["key"] = db["utm_content"].map(content_key)
    db["bucket"] = db["접수"].map(status_bucket) if "접수" in db else "미분류"
    db = db.dropna(subset=["date"])

    # ---- 사이드바
    with st.sidebar:
        st.header("조회 조건")
        dmin, dmax = ad["날짜"].min(), ad["날짜"].max()
        today = dt.date.today()
        preset = st.radio("기간", ["오늘", "어제", "최근 7일", "이번 달", "직접 선택"], index=0)
        if preset == "오늘":
            d0 = d1 = today
        elif preset == "어제":
            d0 = d1 = today - dt.timedelta(days=1)
        elif preset == "최근 7일":
            d0, d1 = today - dt.timedelta(days=6), today
        elif preset == "이번 달":
            d0, d1 = today.replace(day=1), today
        else:
            d0, d1 = st.date_input("날짜 범위", (dmin, dmax), min_value=min(dmin, dmax), max_value=max(dmax, today))
        st.caption(f"광고 데이터 보유: {dmin} ~ {dmax}")

        sets_all = sorted(ad["광고그룹"].dropna().unique().tolist())
        sets = st.multiselect("광고 세트", sets_all, default=sets_all)

        st.subheader("CPC 수정 전 지출 차감")
        st.caption("한 줄에 하나: `소재명 금액`  예) 채무조정_ad2 26400")
        deduct_txt = st.text_area("차감 목록", value="", height=90, label_visibility="collapsed")
        st.subheader("DB 건수 직접 지정")
        st.caption("한 줄에 하나: `소재명 건수`  (시트 집계보다 우선)")
        override_txt = st.text_area("DB 지정", value="", height=70, label_visibility="collapsed")
        if st.button("데이터 새로고침"):
            load_sheet.clear()
            st.rerun()

    def parse_pairs(txt: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for line in (txt or "").splitlines():
            parts = line.replace(":", " ").replace(",", "").split()
            if len(parts) >= 2:
                try:
                    out[parts[0].strip()] = float(parts[1].replace("원", ""))
                except ValueError:
                    pass
        return out

    deduct = parse_pairs(deduct_txt)
    override = parse_pairs(override_txt)

    # ---- 기간·세트 필터
    a = ad[(ad["날짜"] >= d0) & (ad["날짜"] <= d1) & (ad["광고그룹"].isin(sets))]
    b = db[(db["date"] >= d0) & (db["date"] <= d1)]

    # 소재별 집계 (세트·소재명 기준)
    g = (a.groupby(["광고그룹", "소재"], as_index=False)
           .agg(소진=("소진비용", "sum"), 노출=("노출수", "sum"), 클릭=("클릭수", "sum"),
                상태=("상태", "last"), ONOFF=("ON/OFF", "last")))
    g["key"] = g["소재"].map(creative_key)
    g["세트번호"] = g["key"].map(lambda k: k[0] if k else None)

    # DB 건수: (세트번호, 소재번호) 기준 누적
    db_cnt = b.dropna(subset=["key"]).groupby("key").size().to_dict()
    db_bucket = b.dropna(subset=["key"]).groupby(["key", "bucket"]).size().to_dict()   # {(key,bucket): n}
    g["DB"] = g["key"].map(lambda k: db_cnt.get(k, 0)).astype(int)
    for col in ["진행불가", "접수", "미팅", "승인"]:
        g[col] = g["key"].map(lambda k: int(db_bucket.get((k, col), 0)))
    # 같은 (세트, 번호) 키를 가진 소재가 둘 이상이면(예: 채무조정_ad3 / 카카오페이_ad3) 소진이 큰 쪽에만 DB를 배정
    dup = g[g["key"].notna()].groupby("key")["소재"].transform("size") > 1
    if dup.any():
        rank = g.groupby("key")["소진"].rank(method="first", ascending=False)
        loser = dup & (rank > 1)
        for col in ["DB", "진행불가", "접수", "미팅", "승인"]:
            g.loc[loser, col] = 0
    # 지정값·차감 반영
    g["DB"] = g.apply(lambda r: int(override.get(r["소재"], r["DB"])), axis=1)
    g["차감"] = g["소재"].map(lambda n: deduct.get(n, 0.0))
    g["최종소진"] = g["소진"] - g["차감"]
    g["DB단가"] = g.apply(lambda r: round(r["최종소진"] / r["DB"]) if r["DB"] > 0 else None, axis=1)
    g = g.sort_values(["광고그룹", "최종소진"], ascending=[True, False]).reset_index(drop=True)

    # 매칭 안 된 DB (광고 소재 목록에 없는 utm_content)
    known = set(g["key"].dropna())
    unmatched = b[~b["key"].isin(known)]

    # ---- 헤드라인
    tot_spend_raw, tot_deduct = g["소진"].sum(), g["차감"].sum()
    tot_spend = tot_spend_raw - tot_deduct
    tot_db = int(g["DB"].sum())
    cpa = round(tot_spend / tot_db) if tot_db else None
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("최종 소진", f"{tot_spend:,.0f}원", f"-{tot_deduct:,.0f}원 차감" if tot_deduct else None, delta_color="off")
    c2.metric("총 DB", f"{tot_db}건")
    c3.metric("최종 DB단가", f"{cpa:,}원" if cpa else "-")
    c4.metric("접수 이상", f"{int(g[['접수','미팅','승인']].sum().sum())}건")
    c5.metric("미전환 소재 소진", f"{g.loc[g['DB'] == 0, '최종소진'].sum():,.0f}원")
    st.caption(f"기간 {d0} ~ {d1} · 세트 {len(sets)}개 · 소재 {len(g)}개"
               + (f" · ⚠ 광고 소재와 매칭 안 된 DB {len(unmatched)}건" if len(unmatched) else ""))

    tab1, tab2, tab3, tab4 = st.tabs(["소재별", "세트별", "일별 추이", "복사용 리포트"])

    # ---- 소재별
    with tab1:
        show = g[["광고그룹", "소재", "ONOFF", "상태", "소진", "차감", "최종소진", "DB", "DB단가",
                  "진행불가", "접수", "미팅", "승인", "노출", "클릭"]].copy()
        show["DB단가"] = show["DB단가"].map(lambda v: f"{int(v):,}" if pd.notna(v) else "-")
        st.dataframe(show, width="stretch", hide_index=True,
                     column_config={"소진": st.column_config.NumberColumn(format="%,d"),
                                    "차감": st.column_config.NumberColumn(format="%,d"),
                                    "최종소진": st.column_config.NumberColumn(format="%,d"),
                                    "노출": st.column_config.NumberColumn(format="%,d"),
                                    "클릭": st.column_config.NumberColumn(format="%,d")})
        top = g[g["최종소진"] > 0].copy()
        top["label"] = top["소재"]
        if len(top):
            base = alt.Chart(top).encode(y=alt.Y("label:N", sort="-x", title=None))
            ch1 = base.mark_bar(color=COLOR_SPEND, cornerRadiusEnd=4, size=14).encode(
                x=alt.X("최종소진:Q", title="소진(원)"),
                tooltip=["광고그룹", "소재", alt.Tooltip("최종소진:Q", format=","), "DB", "DB단가"])
            ch2 = base.mark_bar(color=COLOR_DB, cornerRadiusEnd=4, size=14).encode(
                x=alt.X("DB:Q", title="DB(건)"),
                tooltip=["광고그룹", "소재", "DB", "DB단가"])
            cc1, cc2 = st.columns(2)
            cc1.altair_chart(ch1.properties(title="소재별 소진", height=22 * len(top) + 40), width="stretch")
            cc2.altair_chart(ch2.properties(title="소재별 DB", height=22 * len(top) + 40), width="stretch")
        if len(unmatched):
            with st.expander(f"광고 소재와 매칭되지 않은 DB {len(unmatched)}건"):
                st.dataframe(unmatched[["date", "utm_campaign", "utm_content", "접수"]], hide_index=True)

    # ---- 세트별
    with tab2:
        s = (g.groupby("광고그룹", as_index=False)
               .agg(소진=("최종소진", "sum"), DB=("DB", "sum"), 진행불가=("진행불가", "sum"),
                    접수=("접수", "sum"), 미팅=("미팅", "sum"), 승인=("승인", "sum"),
                    노출=("노출", "sum"), 클릭=("클릭", "sum")))
        s["DB단가"] = s.apply(lambda r: f"{round(r['소진']/r['DB']):,}" if r["DB"] else "-", axis=1)
        s["진행불가율"] = s.apply(lambda r: f"{r['진행불가']/r['DB']:.1%}" if r["DB"] else "-", axis=1)
        s["접수율"] = s.apply(lambda r: f"{(r['접수']+r['미팅']+r['승인'])/r['DB']:.1%}" if r["DB"] else "-", axis=1)
        st.dataframe(s, width="stretch", hide_index=True,
                     column_config={"소진": st.column_config.NumberColumn(format="%,d"),
                                    "노출": st.column_config.NumberColumn(format="%,d"),
                                    "클릭": st.column_config.NumberColumn(format="%,d")})

    # ---- 일별 추이
    with tab3:
        daily_ad = a.groupby("날짜", as_index=False).agg(소진=("소진비용", "sum"))
        daily_db = b.groupby("date").size().rename("DB").reset_index().rename(columns={"date": "날짜"})
        daily = daily_ad.merge(daily_db, on="날짜", how="outer").fillna(0).sort_values("날짜")
        daily["DB단가"] = daily.apply(lambda r: round(r["소진"] / r["DB"]) if r["DB"] else None, axis=1)
        if len(daily):
            l1 = alt.Chart(daily).mark_line(color=COLOR_SPEND, strokeWidth=2, point=alt.OverlayMarkDef(size=60)).encode(
                x=alt.X("날짜:T", title=None), y=alt.Y("소진:Q", title="소진(원)"),
                tooltip=[alt.Tooltip("날짜:T"), alt.Tooltip("소진:Q", format=","), "DB"])
            l2 = alt.Chart(daily).mark_line(color=COLOR_DB, strokeWidth=2, point=alt.OverlayMarkDef(size=60)).encode(
                x=alt.X("날짜:T", title=None), y=alt.Y("DB:Q", title="DB(건)"),
                tooltip=[alt.Tooltip("날짜:T"), "DB", "DB단가"])
            cc1, cc2 = st.columns(2)
            cc1.altair_chart(l1.properties(title="일별 소진", height=260), width="stretch")
            cc2.altair_chart(l2.properties(title="일별 DB", height=260), width="stretch")
            st.dataframe(daily, hide_index=True, width="stretch",
                         column_config={"소진": st.column_config.NumberColumn(format="%,d")})

    # ---- 복사용 리포트
    with tab4:
        lines = [f"[소재별 DB 현황] — {d0}" + (f" ~ {d1}" if d1 != d0 else ""), ""]
        for setname, part in g.groupby("광고그룹", sort=False):
            lines.append(f"■ {setname}")
            for _, r in part.iterrows():
                tag = f" ({r['상태']})" if r["상태"] and r["상태"] != "진행중" else ""
                lines.append(f"{r['소재']}{tag}")
                spend_line = f"- 소진: {r['최종소진']:,.0f}원"
                if r["차감"]:
                    spend_line += f" (원 소진 {r['소진']:,.0f}원 − 차감 {r['차감']:,.0f}원)"
                lines.append(spend_line)
                lines.append(f"- DB: {int(r['DB'])}건")
                lines.append(f"- DB단가: {int(r['DB단가']):,}원" if pd.notna(r["DB단가"]) else "- DB단가: -")
                lines.append("")
        lines += ["[합계]", f"- 기존 총 소진: {tot_spend_raw:,.0f}원"]
        lines.append(f"- CPC 수정 전 지출 차감: {tot_deduct:,.0f}원")
        for n, v in deduct.items():
            lines.append(f"  · {n}: {v:,.0f}원")
        lines += [f"- 최종 소진: {tot_spend:,.0f}원", f"- 총 DB: {tot_db}건",
                  f"- 최종 DB단가: {cpa:,}원" if cpa else "- 최종 DB단가: -",
                  "", f"- 미전환 소재 소진: {g.loc[g['DB'] == 0, '최종소진'].sum():,.0f}원"]
        st.code("\n".join(lines), language=None)


if __name__ == "__main__":
    main()
