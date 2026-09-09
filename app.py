"""카카오페이 대시보드.

    streamlit run app.py

광고센터에서 받은 다운로드 파일을 올리면 구글 시트에 기록하고, 그 시트와 전환(DB) 시트를
소재번호로 맞춰 소재별 지출 · 전환수 · 전환단가를 보여 준다.

    다운로드 파일 ─▶ 광고 시트 (당일 탭 / 마감 탭) ─┐
                     전환(DB) 시트 ─────────────────┴─▶ 소재별 집계 표

로컬에서는 `config.json` + `service_account.json`, Streamlit Cloud 에서는 Secrets 를 쓴다.
코드 경로는 하나다.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from core.metrics import (BUCKETS, ad_metrics, build_creative_table,  # noqa: E402
                          parse_pairs, stage_cell, summarize, unmatched_db)
from core.util import today_kst  # noqa: E402
from parsers.adcenter_file import parse_adcenter_file  # noqa: E402
from sources import ad_sheet, db_sheet  # noqa: E402

st.set_page_config(page_title="카카오페이 대시보드", page_icon="📊", layout="wide")

def money(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{v:,.0f}원"


# ================================================================ 설정
def _local_config() -> dict:
    p = HERE / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def conf(key: str, default=None):
    """Secrets 우선, 없으면 config.json. 클라우드와 로컬이 같은 코드로 돌게 한다."""
    try:
        v = st.secrets.get(key, None)
        if v not in (None, ""):
            return v
    except Exception:
        pass
    v = _local_config().get(key)
    return v if v not in (None, "") else default


SA_REQUIRED = ["type", "project_id", "private_key", "client_email", "token_uri"]


def sa_info():
    """Secrets 의 서비스 계정 정보. 없으면 None (그때는 service_account.json 파일을 쓴다).

    구글 라이브러리는 항목이 빠져도 'MalformedError' 라고만 해서 원인을 알 수 없다.
    Secrets 를 부분만 붙여넣는 실수가 잦아 여기서 먼저 걸러 낸다.
    """
    try:
        if "gcp_service_account" not in st.secrets:
            return None
        info = dict(st.secrets["gcp_service_account"])
    except Exception:
        return None

    missing = [k for k in SA_REQUIRED if not str(info.get(k, "")).strip()]
    cut = [k for k in ("private_key", "private_key_id", "client_id")
           if str(info.get(k, "")).strip() in ("...", "…")]
    if not missing and "BEGIN PRIVATE KEY" not in str(info.get("private_key", "")):
        cut.append("private_key")
    if missing or cut:
        st.error("Secrets 의 `[gcp_service_account]` 가 온전하지 않습니다.")
        if missing:
            st.write("빠진 항목: " + ", ".join(f"`{k}`" for k in missing))
        if cut:
            st.write("값이 잘린 항목: " + ", ".join(f"`{k}`" for k in sorted(set(cut))))
        st.info("**`cloud_secrets.txt` 파일 전체**를 복사해 붙여넣어 주세요 "
                "(`private_key` 는 `-----BEGIN PRIVATE KEY-----` 로 시작하는 1,700자쯤 되는 값입니다).\n\n"
                "Manage app > Settings > Secrets 에서 고친 뒤 저장하면 앱이 다시 시작됩니다.")
        st.stop()
    return info


def require_setup() -> None:
    """시트 주소가 있어야 시작할 수 있다."""
    if conf("ad_sheet_id"):
        return
    st.title("카카오페이 대시보드")
    st.error("광고 시트가 연결되지 않았습니다.")
    st.markdown(
        "이 앱은 구글 시트에 기록된 광고·전환 데이터를 읽습니다.\n\n"
        "- **Streamlit Cloud**: Manage app > Settings > Secrets 에 `cloud_secrets.txt` 내용 붙여넣기\n"
        "- **내 PC**: `config.json` 의 `ad_sheet_id` 를 채우고 `service_account.json` 을 이 폴더에 두기")
    st.stop()


def gate() -> None:
    """`app_password` 가 설정돼 있으면 비밀번호를 받는다. 없으면 잠그지 않는다."""
    pw_set = conf("app_password", "")
    if not pw_set or st.session_state.get("auth"):
        return
    st.title("카카오페이 대시보드")
    pw = st.text_input("비밀번호", type="password")
    if pw and pw == pw_set:
        st.session_state["auth"] = True
        st.rerun()
    elif pw:
        st.error("비밀번호가 다릅니다.")
    st.stop()


# ================================================================ 데이터
def _ad_args() -> dict:
    return {"today_ws": conf("ad_worksheet_today", ad_sheet.TODAY_WORKSHEET),
            "closed_ws": conf("ad_worksheet_closed", ad_sheet.CLOSED_WORKSHEET),
            "creds_info": sa_info()}


@st.cache_data(ttl=120, show_spinner="시트에서 불러오는 중…")
def load_ad(start=None, end=None) -> pd.DataFrame:
    return ad_sheet.read_split(conf("ad_sheet_id"), start=start, end=end, **_ad_args())


@st.cache_data(ttl=120, show_spinner="시트에서 불러오는 중…")
def load_db(start=None, end=None) -> pd.DataFrame:
    d = db_sheet.from_service_account(
        sheet_id=conf("db_sheet_id", db_sheet.DEFAULT_SHEET_ID), creds_info=sa_info())
    if start is not None:
        d = d[d["날짜"].notna() & (d["날짜"] >= start)]
    if end is not None:
        d = d[d["날짜"].notna() & (d["날짜"] <= end)]
    return d.reset_index(drop=True)


def available_dates() -> list[str]:
    df = load_ad()
    return sorted({str(d) for d in df["날짜"].dropna()}, reverse=True) if len(df) else []


def panel_upload() -> None:
    """광고센터 다운로드 파일을 받아 구글 시트에 기록한다.

    오늘 날짜는 당일 탭, 지난 날짜는 마감 탭. 올린 파일에 든 날짜만 건드린다.
    """
    st.caption("광고센터 소재 화면 우측 상단 **다운로드** 로 받은 파일(xlsx/csv). "
               "**당일 날짜의 데이터만 올려야 합니다.**")
    files = st.file_uploader("광고센터 파일", type=["xlsx", "xls", "csv"],
                             accept_multiple_files=True, key="files")
    if not files:
        return
    fixed = st.date_input("파일명에 날짜가 없을 때 쓸 날짜", value=today_kst(), key="fixed_date",
                          help="파일명의 `_20260909_20260909` 를 우선 씁니다.")
    if not st.button("시트에 올리기", type="primary", key="upload"):
        return

    frames, msgs = [], []
    for f in files:
        try:
            res = parse_adcenter_file(f, filename=f.name)
            if res["start"] is None:                 # 파일명에 날짜가 없으면 지정한 날짜로
                f.seek(0)
                res = parse_adcenter_file(f, filename=f.name, date=fixed)
            frames.append(res["df"])
            msgs.append(f"읽음: {f.name} -> {res['start']} · {len(res['df'])}행 · "
                        f"소진 {res['df']['소진비용'].sum():,.0f}원")
            msgs += [f"   ! {w}" for w in res["warnings"]]
        except Exception as exc:
            msgs.append(f"실패: {f.name} - {exc}")
    if not frames:
        st.error("읽을 수 있는 파일이 없습니다.")
        st.code("\n".join(msgs), language=None)
        return

    try:
        r = ad_sheet.upsert_upload(pd.concat(frames, ignore_index=True),
                                   conf("ad_sheet_id"), **_ad_args())
    except Exception as exc:
        st.error(f"시트에 쓰지 못했습니다: {exc}")
        st.caption("서비스 계정이 시트의 **편집자**로 공유돼 있어야 합니다.")
        return
    st.success(f"시트 반영 완료 · 당일 탭 {r['today']}행 / 마감 탭 {r['closed']}행 "
               f"(날짜 {', '.join(r['dates'])})")
    st.code("\n".join(msgs), language=None)
    st.cache_data.clear()


# 열이 14개라 화면보다 넓어질 수 있다. Streamlit 표는 넘치면 좌우 스크롤이 생기므로,
# 긴 세트명이 가로를 다 먹지 않도록 폭만 잡아 준다.
TABLE_CONFIG = {
    "날짜": st.column_config.TextColumn("날짜", width="small"),
    "광고그룹": st.column_config.TextColumn("광고그룹", width="medium"),
    "소재": st.column_config.TextColumn("소재", width="small"),
    "지출": st.column_config.NumberColumn(format="%,d", width="small"),
    "전환수": st.column_config.NumberColumn(width="small"),
    "전환단가": st.column_config.TextColumn(width="small"),
    "진행불가": st.column_config.TextColumn(width="small"),
    "접수": st.column_config.TextColumn(width="medium"),
    "미팅": st.column_config.TextColumn(width="medium"),
    "승인": st.column_config.TextColumn(width="medium"),
    "CPM": st.column_config.NumberColumn(format="%,d", width="small"),
    "CTR": st.column_config.TextColumn(width="small"),
    "CPC": st.column_config.NumberColumn(format="%,d", width="small"),
    "제출율": st.column_config.TextColumn(width="small"),
}


SUM_COLS = ["지출", "전환수", "노출", "클릭", *BUCKETS]


def totals(g: pd.DataFrame) -> dict:
    """소재별 표를 한 줄로 합친 값. 세트별·일별·합계행이 모두 이걸 쓴다."""
    return {c: (float(g[c].sum()) if len(g) else 0.0) for c in SUM_COLS}


def stage_row(head: dict, v: dict) -> dict:
    """앞쪽 이름칸(head) + 공통 지표. 어느 표든 열 구성이 같아진다.

    진행불가  건수 / 비율
    접수·미팅·승인  건수 / 비율 / 영업단가(지출 ÷ 그 단계 건수)
    CPM · CTR · CPC · 제출율  맨 오른쪽
    """
    spend, conv = float(v["지출"]), int(v["전환수"])
    return {**head,
            "지출": spend,
            "전환수": conv,
            "전환단가": f"{round(spend / conv):,}" if conv else "-",
            "진행불가": stage_cell(v["진행불가"], conv, spend, with_price=False),
            "접수": stage_cell(v["접수"], conv, spend),
            "미팅": stage_cell(v["미팅"], conv, spend),
            "승인": stage_cell(v["승인"], conv, spend),
            **ad_metrics(spend, float(v["노출"]), float(v["클릭"]), conv)}


def creative_table(g: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        stage_row({"광고그룹": r["광고그룹"], "소재": r["소재"],
                   "ON/OFF": r["ONOFF"], "상태": r["상태"]}, r)
        for _, r in g.iterrows()])


def group_table(g: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        stage_row({"광고그룹": name}, totals(part))
        for name, part in g.groupby("광고그룹", sort=False)])


def daily_table(ad: pd.DataFrame, db: pd.DataFrame, override: dict,
                exclude) -> pd.DataFrame:
    """날짜별로 같은 집계를 돌리고 맨 아래에 합계 한 줄을 붙인다."""
    rows, acc = [], {c: 0.0 for c in SUM_COLS}
    for d in sorted({x for x in ad["날짜"] if pd.notna(x)}):
        gd = build_creative_table(ad[ad["날짜"] == d], db[db["날짜"] == d],
                                  db_override=override, exclude_groups=exclude)
        v = totals(gd)
        rows.append(stage_row({"날짜": str(d)}, v))
        for c in SUM_COLS:
            acc[c] += v[c]
    if rows:
        rows.append(stage_row({"날짜": "합계"}, acc))
    return pd.DataFrame(rows)


# ================================================================ 화면
def main() -> None:
    require_setup()
    gate()
    st.title("카카오페이 대시보드")

    c1, c2 = st.columns([1, 4])
    if c1.button("새로고침", type="primary", help="구글 시트를 다시 읽어 화면을 갱신합니다"):
        st.cache_data.clear()
        st.rerun()
    today_df = load_ad(today_kst(), today_kst())
    c2.caption(
        (f"오늘({today_kst()}) 데이터: **{len(today_df)}개 소재 · "
         f"소진 {today_df['소진비용'].sum():,.0f}원**" if len(today_df)
         else "오늘 데이터가 아직 없습니다.")
        + " · 광고센터에서 파일을 받아 올리면 갱신됩니다.")

    dates = available_dates()
    with st.expander("데이터 넣기", expanded=not dates):
        panel_upload()
    if not dates:
        st.info("아직 광고 데이터가 없습니다. 위 **데이터 넣기** 에서 광고센터 다운로드 파일을 올려 주세요.")
        st.stop()

    dmin, dmax = min(dates), max(dates)
    today = today_kst()

    mode = st.radio("조회 모드", ["당일", "일별", "기간"], horizontal=True, index=0)
    if mode == "당일":
        d0 = d1 = today
    elif mode == "일별":
        d0 = d1 = dt.date.fromisoformat(st.selectbox("날짜", dates, index=0))
    else:
        lo, hi = dt.date.fromisoformat(dmin), dt.date.fromisoformat(dmax)
        rng = st.date_input("기간", (lo, hi), min_value=lo, max_value=max(hi, today))
        d0, d1 = (rng if isinstance(rng, tuple) and len(rng) == 2 else (lo, hi))

    ad, db = load_ad(d0, d1), load_db(d0, d1)
    if ad.empty:
        st.warning(f"{d0} ~ {d1} 구간에 광고 데이터가 없습니다. (보유: {dmin} ~ {dmax})")
        st.stop()

    with st.sidebar:
        st.header("보정")
        st.caption("전환수 직접 지정 — 한 줄에 `소재명 건수` (시트 집계보다 우선)")
        override = parse_pairs(st.text_area("전환수지정", height=80, label_visibility="collapsed",
                                            placeholder="채무조정_ad6 12"))
        with_test = st.checkbox("테스트 세트 포함", value=False,
                                help="'테스트' 가 들어간 광고그룹은 기본적으로 집계에서 뺍니다.")
        groups = sorted(ad["광고그룹"].dropna().unique().tolist())
        if not with_test:
            groups = [x for x in groups if "테스트" not in x]
        picked = st.multiselect("광고 세트", groups, default=groups)
        st.divider()
        st.caption(f"광고 데이터 보유: {dmin} ~ {dmax} ({len(dates)}일)")

    ad = ad[ad["광고그룹"].isin(picked)]
    g = build_creative_table(ad, db, db_override=override,
                             exclude_groups=() if with_test else ("테스트",))
    if g.empty:
        st.warning("선택한 조건에 해당하는 소재가 없습니다.")
        st.stop()
    s = summarize(g)
    um = unmatched_db(g, db)

    c = st.columns(5)
    c[0].metric("지출", money(s["지출"]))
    c[1].metric("총 전환수", f"{s['총전환수']}건")
    c[2].metric("전환단가", money(s["전환단가"]))
    c[3].metric("접수", f"{s['접수']}건")
    c[4].metric("미팅", f"{s['미팅']}건")
    st.caption(f"{d0}" + (f" ~ {d1}" if d1 != d0 else "") +
               f" · 세트 {g['광고그룹'].nunique()}개 · 소재 {len(g)}개" +
               (f" · ⚠ 소재와 매칭 안 된 전환 {len(um)}건" if len(um) else "") +
               (" · 이 기간에 들어온 전환 없음" if db.empty else ""))

    tabs = st.tabs(["소재별", "세트별", "일별 추이", "매칭 안 된 전환"])

    with tabs[0]:
        st.dataframe(creative_table(g), width="stretch", hide_index=True,
                     column_config=TABLE_CONFIG)
        st.caption("진행불가는 `건수 / 비율`, 접수·미팅·승인은 `건수 / 비율 / 영업단가` 입니다. "
                   "비율은 전환수 대비, 영업단가는 지출 ÷ 그 단계 건수. "
                   "CPM = 지출÷노출×1000 · CTR = 클릭÷노출 · CPC = 지출÷클릭 · 제출율 = 전환수÷클릭")

    with tabs[1]:
        st.dataframe(group_table(g), width="stretch", hide_index=True,
                     column_config=TABLE_CONFIG)

    with tabs[2]:
        if d0 == d1:
            st.info("기간 모드에서 이틀 이상 선택하면 일별 추이가 나옵니다.")
        else:
            st.dataframe(daily_table(ad, db, override,
                                     () if with_test else ("테스트",)),
                         width="stretch", hide_index=True, column_config=TABLE_CONFIG)
            st.caption("맨 아래 **합계** 는 기간 전체를 합친 값입니다. "
                       "사이드바의 전환수 직접 지정은 날짜마다 그대로 적용됩니다.")

    with tabs[3]:
        if um.empty:
            st.success("모든 전환이 광고 소재와 매칭되었습니다.")
        else:
            st.warning(f"{len(um)}건이 현재 소재 목록과 매칭되지 않았습니다. "
                       "utm_content 오타이거나, 삭제된 소재 또는 선택하지 않은 세트의 전환일 수 있습니다.")
            st.dataframe(um[["날짜", "utm_campaign", "utm_content", "접수상태", "승인상태", "구분"]],
                         width="stretch", hide_index=True)


if __name__ == "__main__":
    main()
