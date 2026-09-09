"""카카오페이 광고 × 전환(DB) 통합 대시보드.

    streamlit run app.py

구조
    광고 데이터  ─ 파일 업로드 / 실시간 수집 / 캡처 OCR ─▶ data/ad_daily.sqlite (날짜+소재 키)
    전환(DB)     ─ 공개 CSV / 파일 업로드 / 서비스 계정 ─▶ data/ad_daily.sqlite (db_rows)
                                                        └─▶ 소재번호로 매칭해 소진·DB·단가
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

import store  # noqa: E402
from core.metrics import (BUCKETS, build_creative_table, parse_pairs,  # noqa: E402
                          report_text, summarize, unmatched_db)
from parsers.adcenter_file import ParseError, parse_adcenter_file  # noqa: E402
from sources import db_sheet  # noqa: E402

st.set_page_config(page_title="카카오페이 광고 × DB 대시보드", page_icon="📊", layout="wide")

# 지시서 지정 색. 라이트 배경에서 6개 검사 통과(CVD ΔE 24.7).
COLOR_SPEND, COLOR_DB = "#2a78d6", "#eb6834"
GRID = {"gridColor": "#e8e8e6", "domainColor": "#d8d8d5", "tickColor": "#d8d8d5",
        "labelColor": "#5a5a58", "titleColor": "#5a5a58"}


def money(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{v:,.0f}원"


# ================================================================ 데이터 소스
# 내 PC: 로컬 sqlite 가 정본. 클라우드: 로컬 파일이 없으니 구글 시트를 읽는다.
# secrets 에 ad_sheet_id 가 있으면 클라우드 모드로 본다.
def _secret(key: str, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def cloud_mode() -> bool:
    return bool(_secret("ad_sheet_id"))


SA_REQUIRED = ["type", "project_id", "private_key", "client_email", "token_uri"]


def _sa_info():
    """secrets 의 서비스 계정 정보. 빠진 항목이 있으면 무엇이 빠졌는지 알려준다.

    구글 라이브러리는 'MalformedError' 라고만 해서 원인을 알 수 없다.
    Secrets 를 부분만 붙여넣는 실수가 잦아 여기서 먼저 걸러 낸다.
    """
    try:
        if "gcp_service_account" not in st.secrets:
            return None
        info = dict(st.secrets["gcp_service_account"])
    except Exception:
        return None

    missing = [k for k in SA_REQUIRED if not str(info.get(k, "")).strip()]
    truncated = [k for k in ("private_key", "private_key_id", "client_id")
                 if str(info.get(k, "")).strip() in ("...", "…")]
    if not missing and "BEGIN PRIVATE KEY" not in str(info.get("private_key", "")):
        truncated.append("private_key")
    if missing or truncated:
        st.error("Secrets 의 `[gcp_service_account]` 가 온전하지 않습니다.")
        if missing:
            st.write("빠진 항목: " + ", ".join(f"`{k}`" for k in missing))
        if truncated:
            st.write("값이 잘린 항목: " + ", ".join(f"`{k}`" for k in set(truncated)))
        st.info(
            "**`cloud_secrets.txt` 파일 전체**를 복사해 붙여넣어 주세요. "
            "화면에 보이는 요약본이 아니라 실제 파일이어야 합니다 "
            "(`private_key` 는 `-----BEGIN PRIVATE KEY-----` 로 시작하는 1,700자 정도의 긴 값입니다).\n\n"
            "Manage app > Settings > Secrets 에서 수정한 뒤 저장하면 앱이 다시 시작됩니다.")
        st.stop()
    return info


@st.cache_data(ttl=120, show_spinner="시트에서 불러오는 중…")
def _sheet_ad(start, end):
    from sources.ad_sheet import read_split
    return read_split(_secret("ad_sheet_id"),
                      today_ws=_secret("ad_worksheet_today", "KakaopayToday"),
                      closed_ws=_secret("ad_worksheet_closed", "KakaopayDaily"),
                      creds_info=_sa_info(), start=start, end=end)


@st.cache_data(ttl=120, show_spinner="시트에서 불러오는 중…")
def _sheet_db(start, end):
    d = db_sheet.from_service_account(creds_info=_sa_info())
    if start is not None:
        d = d[d["날짜"].notna() & (d["날짜"] >= start)]
    if end is not None:
        d = d[d["날짜"].notna() & (d["날짜"] <= end)]
    return d.reset_index(drop=True)


def get_ad(start=None, end=None) -> pd.DataFrame:
    return _sheet_ad(start, end) if cloud_mode() else store.load_ad(start, end)


def get_db(start=None, end=None) -> pd.DataFrame:
    return _sheet_db(start, end) if cloud_mode() else store.load_db(start, end)


def available_dates() -> list[str]:
    if not cloud_mode():
        return store.available_dates()
    df = _sheet_ad(None, None)
    return sorted({str(d) for d in df["날짜"].dropna()}, reverse=True) if len(df) else []


# ================================================================ 데이터 넣기
def panel_ad_file() -> None:
    st.caption("광고센터 소재 화면 우측 상단 **다운로드** 로 받은 파일(xlsx/csv). 여러 개 한꺼번에 가능합니다.")
    files = st.file_uploader("광고센터 파일", type=["xlsx", "xls", "csv"],
                             accept_multiple_files=True, key="ad_files")
    if not files:
        return
    fixed = st.date_input(
        "파일명에 날짜가 없을 때 쓸 날짜", value=dt.date.today(), key="ad_fixed_date",
        help="파일명의 `_20260909_20260909` 를 우선 사용하고, 없을 때만 이 날짜를 씁니다.")
    if not st.button("이 파일들 저장", type="primary", key="save_ad_files"):
        return
    total, msgs = 0, []
    for f in files:
        try:
            res = parse_adcenter_file(f, filename=f.name)
            if res["start"] is None:                    # 파일명에 날짜가 없으면 지정값으로 다시
                f.seek(0)
                res = parse_adcenter_file(f, filename=f.name, date=fixed)
            n = store.upsert_ad(res["df"])
            total += n
            msgs.append(f"✓ {f.name} → {res['start']} · {n}행 · "
                        f"소진 {res['df']['소진비용'].sum():,.0f}원")
            msgs += [f"   ! {w}" for w in res["warnings"]]
        except (ParseError, Exception) as exc:          # noqa: B014
            msgs.append(f"✗ {f.name}: {exc}")
    st.success(f"{total}행 저장") if total else st.error("저장된 행이 없습니다.")
    st.code("\n".join(msgs), language=None)
    st.cache_data.clear()


def panel_ad_live() -> None:
    st.caption("로그인된 크롬 프로필로 광고센터에서 직접 읽어옵니다. **오늘 수치는 실시간 집계라 볼 때마다 달라집니다.**")
    c1, c2 = st.columns(2)
    day = c1.date_input("수집할 날짜", value=dt.date.today(), key="live_date")
    c2.write("")
    c2.write("")
    if c2.button("광고센터에서 지금 가져오기", type="primary", key="go_live"):
        with st.spinner("광고센터에서 읽는 중… (첫 실행은 로그인 창이 필요할 수 있습니다)"):
            try:
                from collect_live import collect
                df = collect([day.isoformat()])
                if df.empty:
                    st.warning("가져온 행이 없습니다. 해당 날짜에 데이터가 없거나 로그인이 필요합니다.")
                else:
                    n = store.upsert_ad(df)
                    st.success(f"{n}행 저장 · 소진 {df['소진비용'].sum():,.0f}원 "
                               f"(수집시각 {df['수집시각'].iloc[0]})")
                    st.cache_data.clear()
            except Exception as exc:
                st.error(f"{exc}")
                st.caption("로그인이 풀렸다면 터미널에서 `python collect_live.py --login` 을 한 번 실행해 주세요.")


def panel_ad_ocr() -> None:
    st.caption("다운로드도 실시간 수집도 안 될 때 쓰는 마지막 수단입니다. "
               "**OCR 은 숫자를 자주 틀리므로 저장 전에 반드시 표를 확인·수정하세요.**")
    img = st.file_uploader("대시보드 캡처 이미지", type=["png", "jpg", "jpeg"], key="ocr_img")
    if not img:
        return
    st.image(img, width=560)
    day = st.date_input("이 캡처의 날짜", value=dt.date.today(), key="ocr_date")
    if st.button("이미지에서 읽기", key="run_ocr"):
        from parsers.adcenter_ocr import OCRUnavailable, extract_rows
        try:
            df, engine, lines = extract_rows(img.getvalue())
            st.session_state["ocr_draft"] = df
            st.session_state["ocr_engine"] = engine
            if df.empty:
                st.warning("소재명을 찾지 못했습니다. 표 부분만 크게 잘라서 다시 올려 보세요.")
                with st.expander("OCR 이 읽은 원문"):
                    st.code("\n".join(lines) or "(빈 결과)", language=None)
        except OCRUnavailable as exc:
            st.error(str(exc))
            return

    draft = st.session_state.get("ocr_draft")
    if draft is not None and not draft.empty:
        st.markdown(f"**확인·수정** (엔진: {st.session_state.get('ocr_engine', '?')}) — "
                    "값을 고친 뒤 저장하세요. 행을 지우면 저장에서 빠집니다.")
        edited = st.data_editor(draft, num_rows="dynamic", width="stretch",
                                key="ocr_editor",
                                column_config={"소진비용": st.column_config.NumberColumn(format="%d")})
        if st.button("확인했습니다 · 저장", type="primary", key="save_ocr"):
            rows = edited.dropna(subset=["소재"])
            rows = rows[rows["소재"].astype(str).str.strip().ne("")]
            if rows.empty:
                st.error("저장할 행이 없습니다.")
            else:
                out = pd.DataFrame({
                    "날짜": day, "소재": rows["소재"].astype(str).str.strip(),
                    "ON/OFF": "", "상태": "", "광고그룹": "(캡처)", "광고상품": "",
                    "소진비용": rows["소진비용"].fillna(0).astype(float),
                    "노출수": 0.0, "클릭수": 0.0, "클릭률": 0.0, "도달수": 0.0,
                    "eCPM": 0.0, "CPC": 0.0, "시작일": "", "종료일": "", "출처": "ocr",
                    "수집시각": dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
                n = store.upsert_ad(out)
                st.success(f"{n}행 저장 (광고그룹은 '(캡처)' 로 들어갑니다)")
                st.session_state.pop("ocr_draft", None)
                st.cache_data.clear()


def panel_db() -> None:
    saved = store.db_saved_at()
    have = len(store.load_db())
    st.caption(f"현재 저장된 전환 데이터: **{have}건**" + (f" · 갱신 {saved}" if saved else ""))

    email = db_sheet.service_account_email()
    has_key = db_sheet.service_account_path().exists()

    st.markdown("**서비스 계정으로 읽기** (기본 · 세션 만료가 없어 무인 자동화에 안정적)")
    if has_key and email:
        st.caption(f"서비스 계정: `{email}` — 이 주소가 시트에 **뷰어**로 공유돼 있어야 합니다.")
        c1, c2 = st.columns([1, 1])
        if c1.button("시트 불러오기", type="primary", key="db_sa"):
            _save_db(lambda: db_sheet.from_service_account())
        if c2.button("설정 점검", key="db_sa_check"):
            for ok, msg in db_sheet.check_service_account():
                (st.success if ok else st.error)(msg)
    else:
        st.warning("`service_account.json` 이 아직 없습니다. README 의 '서비스 계정 설정' 순서를 따라 "
                   "키 파일을 이 폴더에 넣어 주세요.")

    with st.expander("다른 방법 (백업용)"):
        st.caption("서비스 계정이 준비되기 전이나, 일시적으로 막혔을 때 씁니다.")
        f = st.file_uploader("시트 파일 올리기 (시트에서 `파일 > 다운로드 > CSV`)",
                             type=["csv", "xlsx", "xls"], key="db_file")
        if f is not None and st.button("이 파일로 갱신", key="db_upload"):
            _save_db(lambda: db_sheet.from_file(f, filename=f.name))

        st.markdown("**브라우저 세션으로 읽기**")
        cc = st.columns(2)
        if cc[0].button("전용 프로필", key="db_browser"):
            _save_db(lambda: db_sheet.from_browser(mode="profile"))
        if cc[1].button("켜 둔 내 Chrome", key="db_cdp"):
            _save_db(lambda: db_sheet.from_browser(mode="cdp"))

        ok, why = probe_public()
        if ok:
            st.success(why)
            if st.button("공개 시트에서 불러오기", key="db_public"):
                _save_db(lambda: db_sheet.from_public_csv())
        else:
            st.caption(f"공개 CSV 경로: 사용 불가 - {why}")


@st.cache_data(ttl=300, show_spinner=False)
def probe_public() -> tuple[bool, str]:
    return db_sheet.probe_public_csv()


def _save_db(fn) -> None:
    try:
        df = fn()
    except Exception as exc:
        st.error(f"{exc}")
        return
    n = store.save_db(df)
    st.success(f"전환 데이터 {n}건 저장 "
               f"({df['날짜'].min()} ~ {df['날짜'].max()})" if n else "kakaopay 행이 없습니다.")
    st.cache_data.clear()


# ================================================================ 화면
def _panel_collect(dates_known: bool) -> None:
    with st.expander("데이터 넣기", expanded=not dates_known):
        t1, t2, t3, t4 = st.tabs(["① 파일 업로드", "② 실시간 수집", "③ 캡처 이미지", "전환(DB) 시트"])
        with t1:
            panel_ad_file()
        with t2:
            panel_ad_live()
        with t3:
            panel_ad_ocr()
        with t4:
            panel_db()


def gate() -> None:
    """`.streamlit/secrets.toml` 에 app_password 가 있으면 비밀번호를 받는다.

    사무실 네트워크에 열어 두는 경우를 위한 최소한의 잠금. 값이 없으면 잠그지 않는다.
    """
    try:
        pw_set = st.secrets.get("app_password", "")
    except Exception:
        pw_set = ""
    if not pw_set or st.session_state.get("auth"):
        return
    st.title("카카오페이 광고 × 전환(DB) 대시보드")
    pw = st.text_input("비밀번호", type="password")
    if pw and pw == pw_set:
        st.session_state["auth"] = True
        st.rerun()
    elif pw:
        st.error("비밀번호가 다릅니다.")
    st.stop()


def refresh_now() -> None:
    """광고(실시간) + 전환(시트) 을 지금 한 번 가져온다. 스케줄러와 같은 경로."""
    from collect_all import collect_ads, collect_conversions
    msgs = []
    with st.spinner("광고센터와 구글 시트에서 가져오는 중… (20~40초)"):
        ok1, m1 = collect_ads([dt.date.today().isoformat()])
        ok2, m2 = collect_conversions()
        msgs = [(ok1, m1), (ok2, m2)]
    st.session_state["refresh_msgs"] = msgs
    st.cache_data.clear()
    st.rerun()


def main() -> None:
    gate()
    st.title("카카오페이 광고 × 전환(DB) 대시보드")

    c1, c2 = st.columns([1, 4])
    if cloud_mode():
        if c1.button("시트 다시 읽기", type="primary", help="수집기가 올려 둔 최신 시트를 다시 읽습니다"):
            st.cache_data.clear()
            st.rerun()
        c2.caption("수집기는 사무실 PC 에서 30분마다 돌고, 이 화면은 그 결과가 담긴 구글 시트를 읽습니다. "
                   "카카오 로그인이 필요해 수집 자체는 클라우드에서 할 수 없습니다.")
    else:
        if c1.button("지금 새로고침", type="primary",
                     help="광고센터 실시간 수집 + 전환 시트를 함께 가져옵니다"):
            refresh_now()
        at, dbat = store.last_collected_at(dt.date.today()), store.db_saved_at()
        c2.caption(f"광고 마지막 수집 **{at or '없음'}** · 전환 갱신 **{dbat or '없음'}** · "
                   f"자동 수집 30분마다")
    for ok, m in st.session_state.pop("refresh_msgs", []):
        (st.success if ok else st.error)(m)

    dates_known = bool(available_dates())
    # 클라우드에는 카카오 로그인도 로컬 저장소도 없어서 수집 패널이 동작하지 않는다.
    # 눌러도 안 되는 버튼을 두는 대신 어디서 수집되는지 알려준다.
    if cloud_mode():
        st.info("데이터는 사무실 PC 의 수집기가 30분마다 구글 시트에 올리고, 이 화면은 그 시트를 읽습니다. "
                "이 화면에서는 수집하지 않습니다.")
    else:
        _panel_collect(dates_known)


    dates = available_dates()
    if not dates:
        st.info("아직 광고 데이터가 없습니다. 위 **데이터 넣기** 에서 파일을 올리거나 실시간 수집을 눌러 주세요.")
        st.stop()
    dmin, dmax = min(dates), max(dates)
    today = dt.date.today()

    # ---- 조회 모드
    mode = st.radio("조회 모드", ["당일 실시간", "일별", "기간"], horizontal=True, index=0)
    if mode == "당일 실시간":
        d0 = d1 = today
        c1, c2 = st.columns([1, 3])
        if c1.button("광고센터에서 지금 가져오기", type="primary"):
            with st.spinner("광고센터에서 읽는 중…"):
                try:
                    from collect_live import collect
                    df = collect([today.isoformat()])
                    if df.empty:
                        st.warning("가져온 행이 없습니다.")
                    else:
                        store.upsert_ad(df)
                        st.cache_data.clear()
                        st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")
        at = store.last_collected_at(today)
        c2.caption(f"오늘 수집 시각: **{at or '아직 없음'}** · 오늘 수치는 실시간 집계라 볼 때마다 달라집니다.")
    elif mode == "일별":
        pick = st.selectbox("날짜", dates, index=0)
        d0 = d1 = dt.date.fromisoformat(pick)
    else:
        lo, hi = dt.date.fromisoformat(dmin), dt.date.fromisoformat(dmax)
        rng = st.date_input("기간", (lo, hi), min_value=lo, max_value=max(hi, today))
        d0, d1 = (rng if isinstance(rng, tuple) and len(rng) == 2 else (lo, hi))

    ad = get_ad(d0, d1)
    db = get_db(d0, d1)
    if ad.empty:
        st.warning(f"{d0} ~ {d1} 구간에 저장된 광고 데이터가 없습니다. "
                   f"(보유: {dmin} ~ {dmax})")
        st.stop()

    # ---- 사이드바 보정
    with st.sidebar:
        st.header("보정")
        st.caption("CPC 수정 전 지출 차감 — 한 줄에 `소재명 금액`")
        deduct = parse_pairs(st.text_area("차감", height=100, label_visibility="collapsed",
                                          placeholder="채무조정_ad2 26400"))
        st.caption("DB 건수 직접 지정 — 한 줄에 `소재명 건수` (시트 집계보다 우선)")
        override = parse_pairs(st.text_area("DB지정", height=80, label_visibility="collapsed",
                                            placeholder="채무조정_ad6 12"))
        with_test = st.checkbox("테스트 세트 포함", value=False,
                                help="'테스트' 가 들어간 광고그룹은 기본적으로 집계에서 뺍니다.")
        groups = sorted(ad["광고그룹"].dropna().unique().tolist())
        if not with_test:
            groups = [x for x in groups if "테스트" not in x]
        picked = st.multiselect("광고 세트", groups, default=groups)
        st.divider()
        if st.button("저장된 데이터 새로고침"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"광고 데이터 보유: {dmin} ~ {dmax} ({len(dates)}일)")
        st.caption(("데이터 출처: 구글 시트 (클라우드)" if cloud_mode()
                    else f"전환 데이터: {len(store.load_db())}건 · 갱신 {store.db_saved_at() or '없음'}"))

    ad = ad[ad["광고그룹"].isin(picked)]
    g = build_creative_table(ad, db, deduct=deduct, db_override=override,
                             exclude_groups=() if with_test else ("테스트",))
    if g.empty:
        st.warning("선택한 조건에 해당하는 소재가 없습니다.")
        st.stop()
    s = summarize(g)
    um = unmatched_db(g, db)

    # ---- 헤드라인
    c = st.columns(5)
    c[0].metric("최종 소진", money(s["최종소진"]),
                f"-{s['차감']:,.0f}원 차감" if s["차감"] else None, delta_color="off")
    c[1].metric("총 DB", f"{s['총DB']}건")
    c[2].metric("최종 DB단가", money(s["DB단가"]))
    c[3].metric("접수 이상", f"{s['접수이상']}건")
    c[4].metric("미전환 소재 소진", money(s["미전환소재소진"]))
    st.caption(f"{d0}" + (f" ~ {d1}" if d1 != d0 else "") +
               f" · 세트 {g['광고그룹'].nunique()}개 · 소재 {len(g)}개" +
               (f" · ⚠ 소재와 매칭 안 된 DB {len(um)}건" if len(um) else "") +
               (f" · 이 기간에 들어온 전환 없음"
                if db.empty and cloud_mode() else
                (" · 전환 데이터 없음 — '데이터 넣기 > 전환(DB) 시트' 에서 넣어 주세요"
                 if db.empty else "")))

    tabs = st.tabs(["소재별", "세트별", "일별 추이", "매칭 안 된 DB", "복사용 리포트"])

    # ---- 소재별
    with tabs[0]:
        show = g[["광고그룹", "소재", "ONOFF", "상태", "소진", "차감", "최종소진",
                  "DB", "DB단가", *BUCKETS, "노출", "클릭"]].rename(columns={"ONOFF": "ON/OFF"})
        show["DB단가"] = [f"{v:,.0f}" if pd.notna(v) else "-" for v in show["DB단가"]]
        st.dataframe(
            show, width="stretch", hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="%,d")
                           for c in ["소진", "차감", "최종소진", "노출", "클릭"]})

        plot = g[g["최종소진"] > 0].copy()
        if len(plot):
            h = max(200, 24 * len(plot) + 40)
            base = alt.Chart(plot).encode(
                y=alt.Y("소재:N", sort="-x", title=None,
                        axis=alt.Axis(labelLimit=200, **{k: v for k, v in GRID.items()
                                                         if k in ("labelColor", "domainColor", "tickColor")})))
            spend = base.mark_bar(color=COLOR_SPEND, cornerRadiusEnd=4, size=13).encode(
                x=alt.X("최종소진:Q", title="소진(원)", axis=alt.Axis(format="~s")),
                tooltip=[alt.Tooltip("광고그룹:N", title="세트"), alt.Tooltip("소재:N"),
                         alt.Tooltip("최종소진:Q", title="최종소진", format=","),
                         alt.Tooltip("DB:Q"), alt.Tooltip("DB단가:Q", format=",")])
            dbc = base.mark_bar(color=COLOR_DB, cornerRadiusEnd=4, size=13).encode(
                x=alt.X("DB:Q", title="DB(건)"),
                tooltip=[alt.Tooltip("광고그룹:N", title="세트"), alt.Tooltip("소재:N"),
                         alt.Tooltip("DB:Q"), alt.Tooltip("DB단가:Q", format=",")])
            cc = st.columns(2)
            cc[0].altair_chart(spend.properties(title="소재별 소진", height=h)
                               .configure_axis(**GRID).configure_view(strokeWidth=0), width="stretch")
            cc[1].altair_chart(dbc.properties(title="소재별 DB", height=h)
                               .configure_axis(**GRID).configure_view(strokeWidth=0), width="stretch")

    # ---- 세트별
    with tabs[1]:
        t = (g.groupby("광고그룹", as_index=False)
               .agg(소진=("최종소진", "sum"), DB=("DB", "sum"),
                    **{b: (b, "sum") for b in BUCKETS},
                    노출=("노출", "sum"), 클릭=("클릭", "sum")))
        t["DB단가"] = [f"{round(a / b):,}" if b else "-" for a, b in zip(t["소진"], t["DB"])]
        t["진행불가율"] = [f"{a/b:.1%}" if b else "-" for a, b in zip(t["진행불가"], t["DB"])]
        t["접수율"] = [f"{(x+y+z)/b:.1%}" if b else "-"
                     for x, y, z, b in zip(t["접수"], t["미팅"], t["승인"], t["DB"])]
        st.dataframe(t, width="stretch", hide_index=True,
                     column_config={c: st.column_config.NumberColumn(format="%,d")
                                    for c in ["소진", "노출", "클릭"]})

    # ---- 일별 추이
    with tabs[2]:
        if d0 == d1:
            st.info("기간 모드에서 이틀 이상 선택하면 일별 추이가 나옵니다.")
        else:
            da = ad.groupby("날짜", as_index=False).agg(소진=("소진비용", "sum"))
            known = set(g["key"].dropna())
            from core.metrics import content_key
            dd = db.copy()
            dd["key"] = dd["utm_content"].map(content_key)
            dd = dd[dd["key"].isin(known)]
            dbd = (dd.groupby("날짜").size().rename("DB").reset_index()
                   if len(dd) else pd.DataFrame(columns=["날짜", "DB"]))
            daily = da.merge(dbd, on="날짜", how="outer").fillna({"소진": 0, "DB": 0}).sort_values("날짜")
            daily["DB단가"] = [round(a / b) if b else None for a, b in zip(daily["소진"], daily["DB"])]

            def line(col, color, title, unit):
                return (alt.Chart(daily).mark_line(color=color, strokeWidth=2,
                                                   point=alt.OverlayMarkDef(size=70, color=color))
                        .encode(x=alt.X("날짜:T", title=None),
                                y=alt.Y(f"{col}:Q", title=unit),
                                tooltip=[alt.Tooltip("날짜:T"), alt.Tooltip("소진:Q", format=","),
                                         alt.Tooltip("DB:Q"), alt.Tooltip("DB단가:Q", format=",")])
                        .properties(title=title, height=280)
                        .configure_axis(**GRID).configure_view(strokeWidth=0))

            cc = st.columns(2)
            cc[0].altair_chart(line("소진", COLOR_SPEND, "일별 소진", "소진(원)"), width="stretch")
            cc[1].altair_chart(line("DB", COLOR_DB, "일별 DB", "DB(건)"), width="stretch")
            st.dataframe(daily, width="stretch", hide_index=True,
                         column_config={c: st.column_config.NumberColumn(format="%,d")
                                        for c in ["소진", "DB단가"]})
            st.caption("사이드바의 차감·DB 직접 지정은 기간 단위 보정이라 날짜별로 나눌 수 없어 이 탭에는 반영되지 않습니다.")

    # ---- 매칭 안 된 DB
    with tabs[3]:
        if um.empty:
            st.success("모든 DB가 광고 소재와 매칭되었습니다.")
        else:
            st.warning(f"{len(um)}건이 현재 소재 목록과 매칭되지 않았습니다. "
                       "utm_content 오타이거나, 삭제된 소재 또는 선택하지 않은 세트의 DB일 수 있습니다.")
            st.dataframe(um[["날짜", "utm_campaign", "utm_content", "접수", "승인", "구분"]],
                         width="stretch", hide_index=True)

    # ---- 복사용 리포트
    with tabs[4]:
        st.caption("아래 내용을 그대로 복사해 채팅에 붙여넣으세요.")
        st.code(report_text(g, s, d0, d1, deduct), language=None)


if __name__ == "__main__":
    main()
