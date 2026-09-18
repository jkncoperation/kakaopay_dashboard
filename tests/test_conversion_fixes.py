"""전환수 손보정 테스트 — 적힌 날짜·소재만 바뀌고 나머지는 그대로여야 한다."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.metrics import build_creative_table  # noqa: E402
from sources import conversion_fixes  # noqa: E402
from sources.db_sheet import BUCKETS, DB_COLUMNS  # noqa: E402

D18, D17 = dt.date(2026, 9, 18), dt.date(2026, 9, 17)
FIXES = {"2026-09-18": {"채무조정_ad3": 3, "채무조정_ad5": 3}}


def db_row(day, utm, 접수=0):
    r = {c: "" for c in DB_COLUMNS}
    r.update({"날짜": day, "utm_source": "kakaopay", "utm_content": utm, "구분": "미분류"})
    for b in BUCKETS:
        r[b] = 0
    r["접수"] = 접수
    return r


def db(*rows):
    return pd.DataFrame(list(rows), columns=DB_COLUMNS)


def test_소재명을_utm으로_바꾼다():
    assert conversion_fixes.utm_of("채무조정_ad3") == "kakaopay_ad1-3"   # 세트번호 없으면 세트1
    assert conversion_fixes.utm_of("채무조정6_ad9") == "kakaopay_ad6-9"
    assert conversion_fixes.utm_of("이상한이름") is None


def test_모자라면_채운다():
    """9/18 채무조정_ad5 는 시트에 0건 → 3건이 되어야 한다."""
    out = conversion_fixes.apply(db(db_row(D18, "kakaopay_ad1-6")), FIXES)
    assert len(out[out["utm_content"] == "kakaopay_ad1-5"]) == 3
    assert len(out[out["utm_content"] == "kakaopay_ad1-6"]) == 1     # 손대지 않은 소재


def test_넘치면_덜어_낸다():
    rows = [db_row(D18, "kakaopay_ad1-3") for _ in range(5)]
    out = conversion_fixes.apply(db(*rows), FIXES)
    assert len(out[out["utm_content"] == "kakaopay_ad1-3"]) == 3


def test_적힌_날짜만_바꾼다():
    """9/17 은 시트 그대로 1건이어야 한다."""
    out = conversion_fixes.apply(db(db_row(D17, "kakaopay_ad1-3"),
                                    db_row(D18, "kakaopay_ad1-3")), FIXES)
    assert len(out[(out["날짜"] == D17) & (out["utm_content"] == "kakaopay_ad1-3")]) == 1
    assert len(out[(out["날짜"] == D18) & (out["utm_content"] == "kakaopay_ad1-3")]) == 3


def test_보정이_없으면_그대로():
    원본 = db(db_row(D18, "kakaopay_ad1-6"))
    assert len(conversion_fixes.apply(원본, {})) == 1


def test_집계표까지_반영된다():
    """소재별 표의 전환수·전환단가가 보정값을 따라가야 한다."""
    ad = pd.DataFrame([
        {"날짜": D18, "소재": "채무조정_ad3", "광고그룹": "세트1", "소진비용": 108_800,
         "노출수": 1000, "클릭수": 272, "상태": "진행중", "ON/OFF": "OFF"},
        {"날짜": D18, "소재": "채무조정_ad5", "광고그룹": "세트1", "소진비용": 106_000,
         "노출수": 1000, "클릭수": 265, "상태": "진행중", "ON/OFF": "OFF"},
    ])
    원본 = db(db_row(D18, "kakaopay_ad1-3"))          # ad3 만 1건, ad5 는 0건
    전 = build_creative_table(ad, 원본).set_index("소재")
    assert 전.loc["채무조정_ad3", "전환수"] == 1 and 전.loc["채무조정_ad5", "전환수"] == 0

    후 = build_creative_table(ad, conversion_fixes.apply(원본, FIXES)).set_index("소재")
    assert 후.loc["채무조정_ad3", "전환수"] == 3 and 후.loc["채무조정_ad3", "전환단가"] == 36_267
    assert 후.loc["채무조정_ad5", "전환수"] == 3 and 후.loc["채무조정_ad5", "전환단가"] == 35_333


def test_설명_문구():
    assert "2026-09-18" in conversion_fixes.describe(FIXES)
    assert conversion_fixes.describe({}) == ""


def test_실제_파일이_읽힌다():
    """저장소에 넣어 둔 conversion_fixes.json 이 형식에 맞는지."""
    fixes = conversion_fixes.load()
    assert fixes.get("2026-09-18", {}).get("채무조정_ad3") == 3
    assert all(not k.startswith("_") for k in fixes)      # 메모 줄은 걸러진다
