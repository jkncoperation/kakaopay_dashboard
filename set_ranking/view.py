"""'세트 순위' 화면 - 라이브 세트를 예상승인단가 순으로, 세트를 누르면 소재별로.

데이터는 set_ranking/build.py 가 만든 data/set_ranking/*.csv 를 읽는다 (화면에서 API 를 부르지 않는다).
접수·미팅·승인 칸은 다른 화면과 같은 `건수 / 비율 / 영업단가` 이고, 비율은 DB 대비다.
예상승인 = 승인 + 예정미팅 ÷ 2, 예상승인단가 = 지출 ÷ 예상승인.
"""
from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from core.metrics import stage_cell

DATA = Path(__file__).resolve().parent.parent / 'data' / 'set_ranking'
T80, T100 = 800_000, 1_000_000
COLOR = {'80만 이하': '#2e7d32', '100만 이하': '#ef6c00', '100만 초과': '#c62828'}
MUTED = '#8a8a8a'


@st.cache_data(ttl=300, show_spinner=False)
def _load(stamp: float):
    rk = pd.read_csv(DATA / 'ranking.csv')
    cr = pd.read_csv(DATA / 'creatives.csv')
    cv = pd.read_csv(DATA / 'coverage.csv').fillna('')
    meta = json.loads((DATA / 'meta.json').read_text(encoding='utf-8'))
    return rk, cr, cv, meta


def _won(v) -> str:
    return '-' if v is None or pd.isna(v) else f'{v:,.0f}'


def _rows(df: pd.DataFrame, head: list[str]) -> pd.DataFrame:
    """지정 양식 표. 예상승인단가 다음에 지출·DB·디비단가, 그다음 단계별 칸."""
    out = []
    for _, r in df.iterrows():
        sp, n = float(r['spend']), int(r['db'])
        out.append({**{h: r[k] for h, k in head},
                    '구분': r['status'],
                    '예상승인단가': _won(r['exp_cost']),
                    '지출': sp,
                    'DB': n,
                    '디비단가': _won(r['cpl']),
                    '접수': stage_cell(r['recv'], n, sp),
                    '미팅': stage_cell(r['meet'], n, sp),
                    '승인': stage_cell(r['appr'], n, sp),
                    '예정미팅': int(r['pend']),
                    '활성일': int(r['days'])})
    return pd.DataFrame(out)


def _paint(t: pd.DataFrame):
    """구분·예상승인단가 글자색: 80만 이하 초록 / 100만 이하 주황 / 100만 초과 빨강."""
    def col(row):
        c = COLOR.get(row['구분'], MUTED)
        return [f'color: {c}; font-weight: 600' if k in ('구분', '예상승인단가') else '' for k in row.index]
    return t.style.apply(col, axis=1).format({'지출': '{:,.0f}'})


CONFIG = {
    # 순위도 고정해야 세트명 왼쪽에 붙는다. 고정하지 않으면 고정된 세트명이 맨 왼쪽으로
    # 가면서 순위가 그 오른쪽으로 밀린다.
    '순위': st.column_config.NumberColumn(width=50, pinned=True),
    '세트': st.column_config.TextColumn(width=260, pinned=True),
    '소재': st.column_config.TextColumn(width=240, pinned=True),
    '구분': st.column_config.TextColumn(width='small'),
    '예상승인단가': st.column_config.TextColumn(width='small'),
    '디비단가': st.column_config.TextColumn(width='small'),
    '접수': st.column_config.TextColumn(width='medium'),
    '미팅': st.column_config.TextColumn(width='medium'),
    '승인': st.column_config.TextColumn(width='medium'),
}

HELP = ('접수·미팅·승인은 `건수 / 비율 / 영업단가` (비율은 DB 대비, 영업단가는 지출 ÷ 건수). '
        '예상승인 = 승인 + 예정미팅 ÷ 2 · 구분: 예상승인단가 80만원 이하 초록 / 100만원 이하 주황 / 100만원 초과 빨강, '
        '승인·예정미팅이 없으면 접수 유무로 나눕니다.')


TOP_N = 5


def _chart(rk: pd.DataFrame) -> None:
    """상위 5위만. 바마다 간격을 띄우고 오른쪽 끝에 예상승인단가를 적는다."""
    d = rk[rk['exp_cost'].notna()].sort_values('rank').head(TOP_N).copy()
    if d.empty:
        st.info('이 기간에는 승인이나 예정미팅이 있는 라이브 세트가 없습니다.')
        return
    d['label'] = d['rank'].astype(int).astype(str) + '위  ' + d['set']
    d['tag'] = d['exp_cost'].map(lambda v: f'{v:,.0f}원')
    order = d['label'].tolist()

    # 값 글자가 잘리지 않게, 또 80만·100만 선이 보이게 가로 범위를 넉넉히 둔다.
    상한 = max(float(d['exp_cost'].max()) * 1.22, T100 * 1.08)
    # paddingInner 로 바 사이를 띄운다. 전에는 바 높이를 고정해 서로 붙어 보였다.
    base = alt.Chart(d).encode(
        y=alt.Y('label:N', sort=order, title=None,
                scale=alt.Scale(paddingInner=0.4, paddingOuter=0.25),
                axis=alt.Axis(labelLimit=340, labelFontSize=12)))
    bars = base.mark_bar(cornerRadiusEnd=4).encode(
        x=alt.X('exp_cost:Q', title='예상승인단가 (원)', axis=alt.Axis(format=',.0f'),
                scale=alt.Scale(domain=[0, 상한], nice=False)),
        color=alt.Color('status:N', scale=alt.Scale(domain=list(COLOR), range=list(COLOR.values())), legend=alt.Legend(title=None, orient='top')),
        tooltip=[alt.Tooltip('set:N', title='세트'), alt.Tooltip('exp_cost:Q', title='예상승인단가', format=',.0f'),
                 alt.Tooltip('spend:Q', title='지출', format=',.0f'), alt.Tooltip('appr:Q', title='승인'),
                 alt.Tooltip('pend:Q', title='예정미팅')])
    # 값은 바 '안쪽' 오른쪽 끝에 흰색으로. 바 바깥에 두면 배경색 위에 놓여서, 색을
    # 지정하지 않으면 다크 모드에서 검게 나오고 흰색으로 고정하면 밝은 모드에서 사라진다.
    # 바 안쪽은 늘 초록·주황·빨강이라 흰 글자가 두 모드에서 다 읽힌다.
    tags = base.mark_text(align='right', dx=-8, fontSize=12, fontWeight='bold',
                          color='white').encode(
        x=alt.X('exp_cost:Q', scale=alt.Scale(domain=[0, 상한], nice=False)), text='tag:N')
    rules = alt.Chart(pd.DataFrame({'v': [T80, T100]})).mark_rule(strokeDash=[4, 4], color=MUTED).encode(x='v:Q')
    st.altair_chart((bars + rules + tags).properties(height=46 * len(d) + 20), width='stretch')


def render() -> None:
    if not (DATA / 'ranking.csv').exists():
        st.warning('세트 순위 데이터가 아직 없습니다. 이 PC 에서 `python set_ranking/build.py` 를 실행해 주세요.')
        return
    rk_all, cr_all, cv, meta = _load((DATA / 'ranking.csv').stat().st_mtime)

    period = st.segmented_control('기간', ['7일', '14일', '30일'], default='7일', key='rk_period') or '7일'
    rk = rk_all[rk_all['period'] == period].sort_values('rank')
    cr = cr_all[cr_all['period'] == period]
    start, end = rk['start'].min(), rk['end'].max()

    cover = ' · '.join(f"{r['media']} {r['data_end'][5:].replace('-', '/')}" for _, r in cv.iterrows() if r['data_end'])
    st.caption(f"최근 {period} **{start} ~ {meta['end']}** (집계 마지막 날 = 오늘 - 2일) · 데이터 생성 {meta['built_at']}  \n"
               f"비용 데이터 마지막 날: {cover}")

    c = st.columns(4)
    c[0].metric('라이브 세트', f'{len(rk)}개')
    c[1].metric('80만원 이하', f"{(rk['status'] == '80만 이하').sum()}개")
    c[2].metric('100만원 이하', f"{rk['status'].isin(['80만 이하', '100만 이하']).sum()}개")
    top = rk.iloc[0] if len(rk) else None
    c[3].metric('1위', _won(top['exp_cost']) + '원' if top is not None and pd.notna(top['exp_cost']) else '-',
                help=top['set'] if top is not None else None)

    st.session_state.setdefault('rk_focus', None)
    st.session_state.setdefault('rk_ver', 0)
    focus = st.session_state['rk_focus']

    if focus in set(rk['set']):
        b1, b2 = st.columns([1, 5])
        if b1.button('← 세트 순위', key='rk_back'):
            st.session_state['rk_focus'] = None
            st.session_state['rk_ver'] += 1
            st.rerun()
        r = rk[rk['set'] == focus].iloc[0]
        b2.markdown(f"**{focus}** — {int(r['rank'])}위 · 예상승인단가 {_won(r['exp_cost'])}원 · 지출 {r['spend']:,.0f}원 · DB {int(r['db'])}건")
        view = cr[cr['set'] == focus]
        st.dataframe(_paint(_rows(view, [('소재', 'creative')]).assign(마지막집행=view['last'].fillna('').values)),
                     width='stretch', hide_index=True, column_config=CONFIG)
        st.caption(HELP + ' DB만 = 지출 데이터에 없는 소재명으로 들어온 DB.')
        return

    _chart(rk)
    st.caption(f'위 그래프는 상위 {TOP_N}위입니다. 아래 표에서 세트를 클릭하면 그 세트의 '
               '소재별로 들어갑니다. 순위: 예상승인단가 → 접수단가 → 지출.')
    table = _rows(rk, [('순위', 'rank'), ('세트', 'set')])
    ev = st.dataframe(_paint(table), width='stretch', hide_index=True, column_config=CONFIG,
                      on_select='rerun', selection_mode='single-row', key=f"rk_tbl{st.session_state['rk_ver']}")
    picked = list(getattr(getattr(ev, 'selection', None), 'rows', []) or [])
    if picked and picked[0] < len(table):
        st.session_state['rk_focus'] = table.iloc[picked[0]]['세트']
        st.rerun()
    st.caption(HELP)
