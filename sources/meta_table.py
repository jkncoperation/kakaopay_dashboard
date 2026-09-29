"""메타 광고 × 전환 집계. 카카오페이와 같은 지표를 쓰되 3단(캠페인·세트·소재) 으로 묶는다.

카카오페이는 소재명에 세트번호가 박혀 있어(`채무조정3_ad2`) 소재명만으로 세트를 알 수 있지만,
메타는 캠페인 / 광고세트 / 소재가 따로라 그대로 3단으로 둔다.

전환은 소재명(ad_name == utm_content) 으로 붙인다. 소재명이 곧 매칭 키다.
"""
from __future__ import annotations

import pandas as pd

from core.metrics import BUCKETS
from sources.meta_api import match_key

LEVELS = ["캠페인", "광고세트", "소재"]
SUM_COLS = ["지출", "전환수", "노출", "클릭", *BUCKETS]


def build(ads: pd.DataFrame, conv: pd.DataFrame) -> pd.DataFrame:
    """소재 단위 집계표. 캠페인·광고세트를 그대로 달고 있어 어느 단위로든 다시 묶을 수 있다.

    ads: sources.meta_api.fetch 결과 (날짜/캠페인/광고세트/소재/지출/노출수/클릭수)
    conv: sources.meta_api.conversions 결과 (key 열 포함)
    """
    if ads is None or ads.empty:
        return pd.DataFrame(columns=[*LEVELS, *SUM_COLS, "전환단가"])

    g = (ads.groupby(LEVELS, as_index=False)
            .agg(지출=("지출", "sum"), 노출=("노출수", "sum"), 클릭=("클릭수", "sum")))

    if conv is not None and len(conv):
        cnt = conv.groupby("key").size().to_dict()
        sums = {b: conv.groupby("key")[b].sum().to_dict() if b in conv else {} for b in BUCKETS}
    else:
        cnt, sums = {}, {b: {} for b in BUCKETS}

    keys = g["소재"].map(match_key)
    g["전환수"] = keys.map(lambda k: int(cnt.get(k, 0)))
    for b in BUCKETS:
        g[b] = keys.map(lambda k, b=b: int(sums[b].get(k, 0)))

    # 같은 소재명이 여러 캠페인·세트에 걸쳐 있으면 전환이 중복 계산된다.
    # 전환 시트에는 소재명만 있어 어느 세트 것인지 알 수 없으니, 지출이 큰 쪽에만 붙인다.
    dup = g.groupby("소재")["지출"].transform("size") > 1
    if dup.any():
        rank = g.groupby("소재")["지출"].rank(method="first", ascending=False)
        loser = dup & (rank > 1)
        for col in ["전환수", *BUCKETS]:
            g.loc[loser, col] = 0

    g["전환단가"] = [round(s / n) if n > 0 else None for s, n in zip(g["지출"], g["전환수"])]
    return g.sort_values(["캠페인", "광고세트", "지출"],
                         ascending=[True, True, False]).reset_index(drop=True)


def totals(g: pd.DataFrame) -> dict:
    return {c: (float(g[c].sum()) if len(g) else 0.0) for c in SUM_COLS}


def roll_up(g: pd.DataFrame, by: list[str]) -> list[tuple[str, dict]]:
    """by 기준으로 묶어 (이름, 합계) 목록. 지출 큰 순."""
    if not len(g):
        return []
    rows = [(name if isinstance(name, str) else name[-1], totals(part))
            for name, part in g.groupby(by, sort=False)]
    return sorted(rows, key=lambda x: -x[1]["지출"])


def unmatched(g: pd.DataFrame, conv: pd.DataFrame) -> pd.DataFrame:
    """광고 데이터에 없는 소재명으로 들어온 전환 (소재명 변경·삭제 등)."""
    if conv is None or conv.empty:
        return pd.DataFrame(columns=["날짜", "utm_campaign", "utm_content", "구분"])
    known = set(g["소재"].map(match_key)) if len(g) else set()
    return conv[~conv["key"].isin(known)].reset_index(drop=True)
