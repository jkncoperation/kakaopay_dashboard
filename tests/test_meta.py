"""메타 광고 데이터 정규화 · 전환 매칭 · 3단 집계."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sources import meta_api, meta_table  # noqa: E402
from sources.db_sheet import DB_COLUMNS, status_bucket, status_flags  # noqa: E402

D = dt.date(2026, 9, 28)
pytestmark = pytest.mark.filterwarnings("ignore")


def api_row(ad, spend, camp="C1", aset="S1", imp=1000, clicks=10, day=D):
    return {"date_start": day.isoformat(), "campaign_name": camp, "adset_name": aset,
            "ad_name": ad, "ad_id": "1", "spend": str(spend),
            "impressions": str(imp), "clicks": str(clicks)}


def conv_rows(pairs):
    """[(utm_content, 접수상태)] -> 전환 시트 표준 스키마 + key"""
    rows = []
    for content, 접수 in pairs:
        rows.append({"날짜": D, "utm_source": "meta", "utm_campaign": "S1",
                     "utm_content": content, "접수상태": 접수, "승인상태": "",
                     "구분": status_bucket(접수, ""), **status_flags(접수, "")})
    df = pd.DataFrame(rows, columns=DB_COLUMNS) if rows else pd.DataFrame(columns=DB_COLUMNS)
    return meta_api.conversions(df)


# ---------------------------------------------------------------- 정규화
def test_normalize_drops_zero_spend():
    """'지출이 있는 것만' - 계정에 상품이 여러 개 섞여 있어 안 쓴 캠페인은 뺀다."""
    df = meta_api.normalize([api_row("a", 5000), api_row("b", 0)], min_spend=1)
    assert list(df["소재"]) == ["a"]
    assert df.loc[0, "지출"] == 5000 and df.loc[0, "노출수"] == 1000


def test_normalize_columns_and_types():
    df = meta_api.normalize([api_row("a", "1234.56")])
    assert list(df.columns) == meta_api.META_COLUMNS
    assert df.loc[0, "날짜"] == D
    assert abs(df.loc[0, "지출"] - 1234.56) < 1e-9


def test_normalize_empty():
    assert meta_api.normalize([]).empty


def test_conversions_keeps_only_meta_sources():
    rows = [{"날짜": D, "utm_source": s, "utm_campaign": "c", "utm_content": "a",
             "접수상태": "", "승인상태": "", "구분": "미분류",
             "진행불가": 0, "접수": 0, "미팅": 0, "승인": 0}
            for s in ("meta", "facebook", "ig", "kakaopay", "toss")]
    out = meta_api.conversions(pd.DataFrame(rows, columns=DB_COLUMNS))
    assert sorted(out["utm_source"]) == ["facebook", "ig", "meta"]


def test_match_key_ignores_case_and_space():
    assert meta_api.match_key("  대환_비교(test1) ") == meta_api.match_key("대환_비교(TEST1)")


# ---------------------------------------------------------------- 3단 집계
def test_build_matches_conversions_by_ad_name():
    ads = meta_api.normalize([api_row("대환_비교(test1)", 44464),
                              api_row("노말_대화형(test1)", 42473)])
    conv = conv_rows([("대환_비교(test1)", "접수완료"), ("대환_비교(test1)", "미팅확정"),
                      ("노말_대화형(test1)", "자산과다")])
    g = meta_table.build(ads, conv)
    by = g.set_index("소재")
    assert by.loc["대환_비교(test1)", "전환수"] == 2
    assert by.loc["대환_비교(test1)", "전환단가"] == round(44464 / 2)
    assert by.loc["대환_비교(test1)", "접수"] == 2      # 미팅확정은 접수에도 포함
    assert by.loc["대환_비교(test1)", "미팅"] == 1
    assert by.loc["노말_대화형(test1)", "진행불가"] == 1


def test_same_ad_name_in_two_sets_counts_once():
    """전환 시트엔 소재명만 있어 어느 세트 것인지 모른다. 지출 큰 쪽에만 붙인다."""
    ads = meta_api.normalize([api_row("공용소재", 30000, aset="S1"),
                              api_row("공용소재", 10000, aset="S2")])
    g = meta_table.build(ads, conv_rows([("공용소재", "접수완료"), ("공용소재", "접수완료")]))
    assert g["전환수"].sum() == 2                       # 중복 계산 금지
    assert g.loc[g["지출"].idxmax(), "전환수"] == 2


def test_roll_up_levels():
    ads = meta_api.normalize([api_row("a", 3000, camp="C1", aset="S1"),
                              api_row("b", 2000, camp="C1", aset="S2"),
                              api_row("c", 1000, camp="C2", aset="S3")])
    g = meta_table.build(ads, conv_rows([]))
    camps = meta_table.roll_up(g, ["캠페인"])
    assert [n for n, _ in camps] == ["C1", "C2"]         # 지출 큰 순
    assert camps[0][1]["지출"] == 5000
    sets = meta_table.roll_up(g[g["캠페인"] == "C1"], ["광고세트"])
    assert [n for n, _ in sets] == ["S1", "S2"]


def test_unmatched_lists_unknown_ad_names():
    ads = meta_api.normalize([api_row("있는소재", 1000)])
    conv = conv_rows([("있는소재", ""), ("없는소재", "")])
    um = meta_table.unmatched(meta_table.build(ads, conv), conv)
    assert list(um["utm_content"]) == ["없는소재"]


def test_build_empty_ads():
    assert meta_table.build(meta_api.normalize([]), conv_rows([])).empty


def test_credentials_needs_token(monkeypatch, tmp_path):
    monkeypatch.setattr(meta_api, "HERE", tmp_path / "x")
    monkeypatch.delenv("META_SYSTEM_USER_TOKEN", raising=False)
    monkeypatch.delenv("META_AD_ACCOUNT_ID", raising=False)
    with pytest.raises(meta_api.MetaError):
        meta_api.credentials()


def test_credentials_adds_act_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(meta_api, "HERE", tmp_path / "x")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "t")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123")
    assert meta_api.credentials()[1] == "act_123"
