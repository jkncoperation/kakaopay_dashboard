"""세트 영업성과 순위 데이터 만들기 (읽기 전용 집계).

    python set_ranking/build.py              # 마지막 날 = 오늘 - 2일
    python set_ranking/build.py 2026-10-20   # 마지막 날 지정

마지막 날부터 거슬러 최근 7·14·30일 동안, 라이브 중인 세트와 그 소재의 지출 · DB · 접수 ·
미팅 · 승인 · 예정미팅 · 예상승인단가를 계산해 data/set_ranking/ 에 CSV 세 개로 쓴다.
대시보드의 '세트 순위' 화면이 이 파일을 읽는다.

    ranking.csv    기간 × 라이브 세트 (예상승인단가 순위)
    creatives.csv  기간 × 세트 × 소재
    coverage.csv   매체별 비용 데이터 마지막 날짜

지출 출처와 세트 정의는 주간보고서 스크립트(weekly-sales-report 스킬의 weekly_report.py)를
그대로 쓴다: 메타 API, 카카오 모먼트 API, 토스·틱톡 다운로드 보고서, 카카오페이 시트.
영업성과(접수·미팅·승인)는 클로드 공유용 DB RAW 시트 기준이다. 시트·전산은 읽기만 한다.

이 PC 에서만 돈다 (다운로드 폴더의 토스·틱톡 보고서와 로컬 토큰을 쓰기 때문).
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / 'data' / 'set_ranking'
CACHE = HERE / 'cache'
WEEKLY = Path.home() / '.claude' / 'skills' / 'weekly-sales-report' / 'weekly_report.py'
DL = Path.home() / 'Downloads'

# 예상승인단가 4단계 (2026-10-23 지정)
#   80만 이하 초록 / 100만 이하 파랑 / 120만 이하 주황 / 120만 초과 빨강
# 화면 쪽 set_ranking/view.py 의 _tier() 와 같은 기준이어야 한다. 한쪽만 고치면
# 표의 '구분' 과 색이 어긋난다.
T80, T100, T120 = 800_000, 1_000_000, 1_200_000


def tier(exp, exp_cost, recv, spend=1) -> str:
    if exp and exp_cost == exp_cost:
        for 한도, 이름 in ((T80, '80만 이하'), (T100, '100만 이하'), (T120, '120만 이하')):
            if exp_cost <= 한도:
                return 이름
        return '120만 초과'
    if recv:
        return '접수만'
    return '성과 없음' if spend else 'DB만'


def stage(sp, n, r, m, ap, pp) -> dict:
    """지정 양식 숫자: 건수, DB 대비 비율, 단가(지출 ÷ 건수). 예상승인 = 승인 + 예정미팅 ÷ 2."""
    ex = ap + pp / 2
    pct = lambda k: round(k / n, 4) if n else None
    cost = lambda k: round(sp / k) if k and sp else None
    return dict(spend=round(sp), db=n, cpl=cost(n), recv=r, meet=m, appr=ap, pend=pp, exp=ex,
                recv_rate=pct(r), meet_pct=pct(m), appr_pct=pct(ap),
                recv_cost=cost(r), meet_cost=cost(m), appr_cost=cost(ap), exp_cost=cost(ex))


# ---------------------------------------------------------------- 주간보고서 로더
E = pd.Timestamp(sys.argv[1]) if len(sys.argv) > 1 else pd.Timestamp.today().normalize() - pd.Timedelta(days=2)
S30 = E - pd.Timedelta(days=29)
# 로더는 주간 기준 기간만 읽으므로 --end 를 일주일 앞당겨 30일 창까지 받게 하고, --last 로 끝을 E 에 맞춘다.
sys.argv = ['weekly_report.py', '--end', f'{E - pd.Timedelta(days=7):%Y-%m-%d}', '--last', f'{E:%Y-%m-%d}']
src = WEEKLY.read_text(encoding='utf-8')
g = {'__name__': 'set_ranking', '__file__': str(WEEKLY)}
exec(compile(src[:src.index('# 출력 순서')], str(WEEKLY), 'exec'), g)
sys.stdout.reconfigure(encoding='utf-8')
d = g['d']
MASK = {n: mk for n, _, mk in g['SETS']}
MEDIA_OF = lambda n: next(k for k in ['메타', '카카오 메인', '카카오 서브', '카카오페이', '토스', '틱톡'] if n.startswith(k))

# 메타는 API 로 E 까지 받으므로 지출 없는 날(예: 금요일 미집행)이 끝이어도 데이터 끝은 E 다
cov = {'메타': E}
for k, df in g['COVER'].items():
    cov[k] = df['day'].max() if len(df) else pd.NaT
cov = {k: min(v, E) if pd.notna(v) else pd.NaT for k, v in cov.items()}

# 운영자가 오프했다고 알려 준 세트 (데이터상 최근 지출이 남아 있어도 순위에서 뺀다). 다시 켜면 여기서 지운다.
OFF_SETS = {
    '카카오 서브 연체해결 세트 (B)',        # 2026-10-23 오프
    '토스 이미지 소재 세트',                # 2026-10-23 오프
    '토스 adset1 / 간소화 랜딩',            # 2026-10-23 오프
}
# 이름을 바꾼 날부터 새 세트로 본다: {세트: (새로 보는 날, 화면에 쓸 이름)}.
# 카카오 서브 '테스트 소재 세트 / 납입금 절감' 은 2026-10-21 에 '테스트 소재 세트 A' 로 바뀌었다
# (DB 시트의 옛 이름 마지막 신청 10/20 23:37, 새 이름 첫 신청 10/21 17:59). 그 전 지출·DB 는 세지 않는다.
RENAMED = {'카카오 서브 납입금 절감 세트 (A)': (pd.Timestamp('2026-10-21'), '카카오 서브 테스트 소재 세트 A')}

# ---------------------------------------------------------------- 세트 순위
rows = []
for N in (7, 14, 30):
    S0 = E - pd.Timedelta(days=N - 1)
    for name, sp_ser, mask in g['SETS']:
        if name in OFF_SETS:
            continue
        S = max(S0, RENAMED[name][0]) if name in RENAMED else S0
        ce = cov.get(MEDIA_OF(name))
        if pd.isna(ce) or not len(sp_ser):
            continue
        # 비용 데이터 마지막 나흘에 지출이 없으면 오프. 이틀로 보면 금·토 쉬는 세트(메타·카카오페이)가 주말에 빠진다
        if sp_ser[(sp_ser.index > ce - pd.Timedelta(days=4)) & (sp_ser.index <= ce)].sum() <= 0:
            continue
        win = sp_ser[(sp_ser.index >= S) & (sp_ser.index <= ce)]
        sp = float(win.sum())
        if sp < 1:
            continue
        x = d[mask & (d.dt >= S) & (d.dt < ce + pd.Timedelta(days=1))]
        rows.append(dict(period=f'{N}일', set=name, media=MEDIA_OF(name), start=f'{S:%Y-%m-%d}', end=f'{ce:%Y-%m-%d}',
                         days=int((win > 0).sum()),
                         **stage(sp, len(x), int(x.recv.sum()), int(x.meet.sum()), int(x.appr.sum()), int(x.pend.sum()))))
rank = pd.DataFrame(rows)
rank['_t'] = rank.exp.gt(0).map({True: 0, False: 1}) + (rank.recv.eq(0) & rank.exp.eq(0)).astype(int)
rank = rank.sort_values(['period', '_t', 'exp_cost', 'recv_cost', 'spend'], ascending=[True, True, True, True, False], na_position='last')
rank['rank'] = rank.groupby('period').cumcount() + 1
rank['status'] = [tier(e, c, r) for e, c, r in zip(rank.exp, rank.exp_cost, rank.recv)]
rank = rank.drop(columns='_t')

# ---------------------------------------------------------------- 소재별 지출
sp_rows, db_cre = [], {}          # (세트, 소재, 일, 지출) / 세트 → DB 행별 소재명

# 메타: 광고 단위 API
env = g['env']
url = f'https://graph.facebook.com/{env.get("META_GRAPH_VERSION", "v21.0")}/act_{env["META_AD_ACCOUNT_ID"].replace("act_", "")}/insights'
params = {'access_token': env['META_SYSTEM_USER_TOKEN'], 'level': 'ad', 'time_increment': 1, 'limit': 500,
          'time_range': json.dumps({'since': f'{S30:%Y-%m-%d}', 'until': f'{E:%Y-%m-%d}'}),
          'fields': 'date_start,adset_name,ad_name,ad_id,spend'}
mrows = []
while url:
    j = requests.get(url, params=params, timeout=90).json()
    mrows += j.get('data', [])
    url, params = j.get('paging', {}).get('next'), None
mi = pd.DataFrame(mrows)
META_SET = lambda a: ('메타 노말_본세트' if a == '노말_본세트' else '메타 고성과 카피 간소화 랜딩 세트' if '토스_form2' in a
                      else '메타 고성과 카피 간소화 인스턴트 세트' if '카카오_v13' in a
                      else '메타 신규 소재_테스트 세트' if a == '신규 소재_테스트 세트' else None)
if len(mi):
    mi['spend'] = mi.spend.astype(float)
    mi['set'] = mi.adset_name.map(META_SET)
    sp_rows += [(r.set, r.ad_name, pd.Timestamp(r.date_start), r.spend) for r in mi[mi.set.notna()].itertuples()]
    adid = dict(zip(mi.ad_id, mi.ad_name))
    for s in mi.set.dropna().unique():
        db_cre[s] = d[MASK[s]].utm_content.str.strip().map(lambda k: adid.get(k, k))

# 카카오 메인·서브: 소재 단위 리포트 API (31일 이하라 한 번에)
sys.path.insert(0, str(WEEKLY.parent))
import kakao_moment as km  # noqa: E402

KSET = [(802036, [('연체 해결', '카카오 메인 연체해결 세트'), ('납입금 절감', '카카오 메인 납입금 절감 세트')]),
        # 2026-10-22 광고그룹 이름 변경: 납입금 절감 → 테스트 소재 세트 A, 연체 해결 → B
        (928346, [('테스트 소재 세트 A', '카카오 서브 납입금 절감 세트 (A)'), ('테스트 소재 세트 / 납입금', '카카오 서브 납입금 절감 세트 (A)'),
                  ('테스트 소재 세트 B', '카카오 서브 연체해결 세트 (B)'), ('테스트 소재 세트 / 연체', '카카오 서브 연체해결 세트 (B)')])]
CACHE.mkdir(exist_ok=True)
kcache = CACHE / f'kakao_cre_{S30:%Y%m%d}_{E:%Y%m%d}.pkl'
if kcache.exists():
    kk, kname = pd.read_pickle(kcache)
else:
    tok, kr, kname = km._token(), [], {}
    for acct, rules in KSET:
        for c in km._get('/campaigns', acct, tok).get('content') or []:
            for grp in km._get('/adGroups', acct, tok, campaignId=c['id']).get('content') or []:
                lab = next((l for k, l in rules if k in grp['name']), None)
                if not lab:
                    continue
                nm = {str(x['id']): x['name'] for x in km._get('/creatives', acct, tok, adGroupId=grp['id']).get('content') or []}
                kname.update(nm)
                ids = list(nm)
                for i in range(0, len(ids), 50):
                    b = km._get('/creatives/report', acct, tok, creativeId=','.join(ids[i:i + 50]), timeUnit='DAY',
                                start=f'{S30:%Y%m%d}', end=f'{E:%Y%m%d}', metricsGroup='BASIC')
                    for r in b.get('data') or []:
                        cost = float((r.get('metrics') or {}).get('cost') or 0)
                        if cost > 0:
                            kr.append(dict(set=lab, cid=str((r.get('dimensions') or {}).get('creative_id')), day=pd.Timestamp(r['start']), cost=cost))
    kk = pd.DataFrame(kr, columns=['set', 'cid', 'day', 'cost'])
    pd.to_pickle((kk, kname), kcache)


def kakao_cre(name: str) -> str:
    """'[비즈니스폼+] new kakao123_1029x258 / 간편양식' → 'kakao123'. 960x300 은 따로 표시."""
    m = re.search(r'(kakao\d+)', name or '')
    return (m.group(1) if m else name) + (' (960x300)' if '960x300' in (name or '') else '')


sp_rows += [(r.set, kakao_cre(kname.get(r.cid, r.cid)), r.day, r.cost) for r in kk.itertuples()]
for s in {l for _, rules in KSET for _, l in rules}:
    def _k(v):
        m = re.match(r'미등록 소재 ID: (\d+)', v)           # 비즈폼에 등록 전인 새 소재
        return kakao_cre(kname.get(m.group(1), v) if m else v)
    db_cre[s] = d[MASK[s]].utm_content.str.strip().map(_k)


# 카카오페이: 시트 소재 ↔ DB 소재 코드. 지출(시트 소재명)과 DB(utm_content) 양쪽에 같이
# 걸어서 하나의 이름으로 모은다. 시트 소재명은 규칙에 안 걸려 그대로 통과한다.
#
#   DB kakaopay_ad1-6     -> 채무조정_ad6
#   DB kakaopay_ad6-12    -> 채무조정6_ad12
#   DB 2nd_kakaopay_ad5   -> 2캠페인_채무조정_ad5     <- 하이픈이 없는 별도 캠페인 계열
#
# 2nd 를 빠뜨리면 지출은 '2캠페인_채무조정_ad5', DB 는 '2nd_kakaopay_ad5' 로 갈려서
# 한 소재가 '지출만' 과 'DB만' 두 줄로 보인다. core/metrics.py 의 PREFIX_FAMILY 와
# 같은 대응이니 한쪽만 고치지 말 것.
def kp_key(v) -> str:
    v = str(v).strip()
    m = re.search(r'(?:^|_)2nd_kakaopay_ad(\d+)$', v, re.I)
    if m:
        return f'2캠페인_채무조정_ad{m.group(1)}'
    m = re.search(r'ad(\d+)-(\d+)$', v)
    return f"채무조정{'' if m.group(1) == '1' else m.group(1)}_ad{m.group(2)}" if m else v


kp = g['kp']
sp_rows += [('카카오페이', kp_key(r['소재']), r['day'], r['cost']) for _, r in kp[kp.cost > 0].iterrows()]
db_cre['카카오페이'] = d[MASK['카카오페이']].utm_content.str.strip().map(kp_key)

# 토스: 일별 보고서 우선, 없는 날은 소재별 시간별 보고서 (같은 날·시간은 최신 파일)
fr = []
for pat, hourly in (('광고 시트용 보고서*.csv', False), ('토스 소재별 시간별 보고서*.csv', True)):
    for f in glob.glob(str(DL / pat)):
        x = g['read_any'](f)
        if x is None or '소재명' not in x.columns:
            continue
        x['day'] = pd.to_datetime(x['이벤트 발생 날짜'], errors='coerce')
        x['hr'] = x['이벤트 발생 시간'].astype(str).str.slice(0, 19) if hourly and '이벤트 발생 시간' in x.columns else 'daily'
        x['cost'] = pd.to_numeric(x['집행 비용 (VAT 제외) (₩)'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        x['_src'], x['_daily'] = os.path.getmtime(f), not hourly
        x['tset'], x['ad'] = x['광고세트명'].astype(str).str.strip(), x['소재명'].astype(str).str.strip()
        fr.append(x[x.day.notna()][['day', 'hr', 'tset', 'ad', 'cost', '_src', '_daily']])
ta = pd.concat(fr, ignore_index=True)
dly = ta[ta._daily]
dly = dly[dly._src == dly.day.map(dly.groupby('day')._src.max())]
hrl = ta[~ta._daily & ~ta.day.isin(set(dly.day))]
hrl = hrl[hrl._src == hrl.set_index(['day', 'hr']).index.map(hrl.groupby(['day', 'hr'])._src.max())]
tt = pd.concat([dly, hrl], ignore_index=True)

TOSS_ALIAS = {'잠재양식 신규 세트': '잠재양식 신규 세트 - 대환, 채무통합', '잠재양식 신규 세트2 - 대환, 채무통합': '잠재양식 신규 세트 - 대환, 채무통합'}


def toss_set(s: str):
    """보고서 광고세트명 → 대시보드 세트 이름 (간소화 랜딩 adsetN 은 이름의 시간대가 바뀌어도 하나로)."""
    m = re.match(r'adset(\d) /', s)
    if m:
        return f'토스 adset{m.group(1)} / 간소화 랜딩'
    s = TOSS_ALIAS.get(s, s)
    return {'잠재양식 신규 세트 - 대환, 채무통합': '토스 잠재양식 신규 세트 - 대환, 채무통합',
            '잠재양식 신규 세트2 - 채무조정': '토스 잠재양식 신규 세트2 - 채무조정',
            '토스 이미지 소재 세트': '토스 이미지 소재 세트'}.get(s)


def adno(name: str):
    """랜딩 소재 번호: 보고서 '간소화 랜딩 소재2 ad1' ↔ DB 'new_toss_landing_ad2-1'."""
    for pat in (r'간소화 랜딩 소재\d* ad(\d+)$', r'신버전 랜딩 소재 \d+ ad(\d+)$', r'simplified_toss_landing_ad\d+-(\d+)$'):
        m = re.search(pat, name)
        if m:
            return 'ad' + m.group(1)
    m = re.search(r'new_toss_landing_ad(\d+)(?:-(\d+))?$', name)
    return 'ad' + (m.group(2) or m.group(1)) if m else None


tt['lab'] = tt.tset.map(toss_set)
tt = tt[tt.lab.notna() & (tt.cost > 0)]
sp_rows += [(r.lab, r.ad, r.day, r.cost) for r in tt.itertuples()]
for lab in tt.lab.unique():
    rep = set(tt[tt.lab == lab].ad)

    def _t(v, rep=rep):
        if v in rep:
            return v
        c = [r for r in rep if v and r.split(' - ')[-1] == v.split(' - ')[-1]]     # 'ad27' 처럼 꼬리 번호가 같은 소재
        if len(c) != 1 and adno(v):
            c = [r for r in rep if adno(r) == adno(v)]
        return c[0] if len(c) == 1 else v
    db_cre[lab] = d[MASK[lab]].utm_content.str.strip().map(_t)

# 틱톡: 일일보고서(광고 이름 포함) xlsx
fr = []
for f in glob.glob(str(DL / 'Tiktok Ads_일일보고서*.xlsx')):
    try:
        x = pd.read_excel(f)
    except Exception:
        continue
    if '광고 이름' not in x.columns:
        continue
    x['day'] = pd.to_datetime(x['일별'], errors='coerce')
    x['cost'] = pd.to_numeric(x['지출'], errors='coerce').fillna(0)
    x['_src'] = os.path.getmtime(f)
    x['grp'] = x['광고 그룹 이름'].astype(str).str.strip() if '광고 그룹 이름' in x.columns else ''
    fr.append(x[x.day.notna()][['day', 'grp', '광고 이름', 'cost', '_src']])
if fr:
    tk = pd.concat(fr)
    tk = tk[tk._src == tk.day.map(tk.groupby('day')._src.max())]
    tk = tk[tk.grp == '대화형 영상 소재']          # 세트 정의와 같게: 이 광고그룹의 소재만 (메신저형·통화형·대화형)
    sp_rows += [('틱톡 대화형 영상 소재', str(r['광고 이름']).strip(), r['day'], r['cost']) for _, r in tk[tk.cost > 0].iterrows()]
db_cre['틱톡 대화형 영상 소재'] = d[MASK['틱톡 대화형 영상 소재']].utm_content.str.strip().replace({'__CID_NAME__': '(소재명 미확인)'})

# ---------------------------------------------------------------- 소재별 집계
spd = pd.DataFrame(sp_rows, columns=['set', 'cre', 'day', 'cost'])
spd['day'] = pd.to_datetime(spd.day)
out = []
for _, rr in rank.iterrows():
    per, s, S, end = rr['period'], rr['set'], pd.Timestamp(rr['start']), pd.Timestamp(rr['end'])
    a = spd[(spd.set == s) & (spd.day >= S) & (spd.day <= end)]
    x = d[MASK[s] & (d.dt >= S) & (d.dt < end + pd.Timedelta(days=1))].copy()
    x['cre'] = db_cre.get(s, pd.Series(dtype=str)).reindex(x.index).fillna('').replace('', '(소재명 없음)')
    for c in set(a.cre) | set(x.cre):
        aa, xx = a[a.cre == c], x[x.cre == c]
        out.append(dict(period=per, set=s, creative=c, days=int(aa.day.nunique()),
                        last=f'{aa.day.max():%Y-%m-%d}' if len(aa) else '',
                        **stage(float(aa.cost.sum()), len(xx), int(xx.recv.sum()), int(xx.meet.sum()), int(xx.appr.sum()), int(xx.pend.sum()))))
cre = pd.DataFrame(out)
cre['status'] = [tier(e, c, r, s) for e, c, r, s in zip(cre.exp, cre.exp_cost, cre.recv, cre.spend)]
cre['_t'] = cre.exp.gt(0).map({True: 0, False: 1}) + (cre.recv.eq(0) & cre.exp.eq(0)).astype(int)
cre = cre.sort_values(['period', 'set', '_t', 'exp_cost', 'recv_cost', 'spend'], ascending=[True, True, True, True, True, False],
                      na_position='last').drop(columns='_t')

# 이름을 바꾼 세트는 새 이름으로 보여 준다 (집계는 위에서 바꾼 날부터만 했다)
LABEL = {k: v for k, (_, v) in RENAMED.items()}
rank['set'] = rank['set'].replace(LABEL)
cre['set'] = cre['set'].replace(LABEL)

# ---------------------------------------------------------------- 저장
OUT.mkdir(parents=True, exist_ok=True)
rank.to_csv(OUT / 'ranking.csv', index=False, encoding='utf-8')
cre.to_csv(OUT / 'creatives.csv', index=False, encoding='utf-8')
pd.DataFrame([{'media': k, 'data_end': f'{v:%Y-%m-%d}' if pd.notna(v) else ''} for k, v in cov.items()]).to_csv(
    OUT / 'coverage.csv', index=False, encoding='utf-8')
(OUT / 'meta.json').write_text(json.dumps({'end': f'{E:%Y-%m-%d}', 'built_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')},
                                          ensure_ascii=False), encoding='utf-8')

# 소재 합계가 세트 합계와 맞는지 (안 맞으면 소재 매칭이 빠진 것)
chk = cre.groupby(['period', 'set'])[['spend', 'db']].sum().join(rank.set_index(['period', 'set'])[['spend', 'db']], rsuffix='_set')
bad = chk[(chk.spend != chk.spend_set) | (chk.db != chk.db_set)]
print(f'마지막 날 {E:%Y-%m-%d} · 세트 {rank.groupby("period").size().to_dict()} · 소재 {cre.groupby("period").size().to_dict()}')
print('매체별 비용 데이터 마지막 날', {k: f'{v:%m/%d}' for k, v in cov.items() if pd.notna(v)})
print('세트·소재 합계 불일치' if len(bad) else '세트·소재 합계 일치', bad.to_string() if len(bad) else '')
