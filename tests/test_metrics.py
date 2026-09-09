"""매칭·계산 규칙 검증 (지시서 2절)."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.metrics import (build_creative_table, content_key, creative_key,  # noqa: E402
                          parse_pairs, report_text, summarize, unmatched_db)
from sources.db_sheet import status_bucket, status_flags  # noqa: E402

D = dt.date(2026, 9, 9)


def ad_row(소재, 광고그룹, 소진, 클릭=0, 상태="진행중", onoff="ON"):
    return {"날짜": D, "소재": 소재, "ON/OFF": onoff, "상태": 상태, "광고그룹": 광고그룹,
            "광고상품": "Fit 배너", "소진비용": 소진, "노출수": 0.0, "클릭수": 클릭,
            "클릭률": 0.0, "도달수": 0.0, "eCPM": 0.0, "CPC": 0.0,
            "시작일": "", "종료일": "", "출처": "test", "수집시각": ""}


def db_row(content, 접수="", 승인=""):
    return {"날짜": D, "utm_source": "kakaopay", "utm_campaign": "kakaopay_adset",
            "utm_content": content, "접수상태": 접수, "승인상태": 승인,
            "구분": status_bucket(접수, 승인), **status_flags(접수, 승인)}


# ---------------------------------------------------------------- 키 매칭
@pytest.mark.parametrize("name,expected", [
    ("채무조정_ad6", (1, 6)),        # 세트번호 없는 접두어는 세트1
    ("채무조정2_ad6", (2, 6)),
    ("채무조정3_ad2", (3, 2)),
    ("채무조정4_ad1", (4, 1)),
    ("카카오페이_ad3", (1, 3)),
    ("카카오페이_ad1_사진변경", None),   # 규칙에 안 맞음
    ("테스트 소재", None),
])
def test_creative_key(name, expected):
    assert creative_key(name) == expected


@pytest.mark.parametrize("content,expected", [
    ("kakaopay_ad1-6", (1, 6)),
    ("kakaopay_ad2-5", (2, 5)),
    ("kakaopay_ad3-2", (3, 2)),
    ("KAKAOPAY_AD4-11", (4, 11)),
    ("naver_ad1-6", None),
    ("", None),
])
def test_content_key(content, expected):
    assert content_key(content) == expected


def test_creative_and_content_keys_agree():
    """채무조정2_ad6 == kakaopay_ad2-6 (세트가 다르면 다른 소재)"""
    assert creative_key("채무조정2_ad6") == content_key("kakaopay_ad2-6")
    assert creative_key("채무조정_ad6") == content_key("kakaopay_ad1-6")
    assert creative_key("채무조정_ad6") != content_key("kakaopay_ad2-6")


# ---------------------------------------------------------------- 상태 분류
@pytest.mark.parametrize("접수,승인,expected", [
    ("자산과다", "", "진행불가"),
    ("소득 증빙 불가", "", "진행불가"),      # 공백 제거 후 비교
    ("장기 연체", "", "진행불가"),
    ("접수완료", "", "접수"),
    ("미팅보류", "", "접수"),
    ("미팅확정", "", "미팅"),
    ("미팅 확정v2", "", "미팅"),
    ("미팅 확정v3", "", "미팅"),
    ("접수완료", "승인예정", "승인"),        # 승인 열이 최우선
    ("미팅확정", "승인완료", "승인"),
    ("", "", "미분류"),
    ("기타메모", "", "미분류"),
])
def test_status_bucket(접수, 승인, expected):
    """표시용 라벨 - 뒷단계가 우선"""
    assert status_bucket(접수, 승인) == expected


@pytest.mark.parametrize("접수,승인,expected", [
    # 미팅확정은 접수에도 포함된다 (깔때기: 뒷단계는 앞단계를 포함)
    ("미팅확정", "", {"진행불가": 0, "접수": 1, "미팅": 1, "승인": 0}),
    ("미팅 확정v2", "", {"진행불가": 0, "접수": 1, "미팅": 1, "승인": 0}),
    ("미팅 확정v3", "승인완료", {"진행불가": 0, "접수": 1, "미팅": 1, "승인": 1}),
    ("접수완료", "", {"진행불가": 0, "접수": 1, "미팅": 0, "승인": 0}),
    ("장기 연체", "", {"진행불가": 1, "접수": 0, "미팅": 0, "승인": 0}),
    ("", "", {"진행불가": 0, "접수": 0, "미팅": 0, "승인": 0}),
])
def test_status_flags_are_cumulative(접수, 승인, expected):
    assert status_flags(접수, 승인) == expected


# ---------------------------------------------------------------- 집계
def test_basic_aggregation_and_db_price():
    ad = pd.DataFrame([ad_row("채무조정_ad6", "세트1", 51000, 480),
                       ad_row("채무조정_ad2", "세트1", 26400, 300),
                       ad_row("채무조정2_ad9", "세트2", 12000, 95)])
    db = pd.DataFrame([db_row("kakaopay_ad1-6", "접수완료"),
                       db_row("kakaopay_ad1-6", "", "승인예정"),
                       db_row("kakaopay_ad1-2", "자산과다")])
    g = build_creative_table(ad, db)
    by = g.set_index("소재")
    assert by.loc["채무조정_ad6", "DB"] == 2
    assert by.loc["채무조정_ad6", "DB단가"] == 25500        # 51000 / 2
    assert by.loc["채무조정_ad6", "승인"] == 1
    assert by.loc["채무조정_ad2", "진행불가"] == 1
    assert by.loc["채무조정2_ad9", "DB"] == 0
    assert pd.isna(by.loc["채무조정2_ad9", "DB단가"])       # 0건은 '-' 로 표시

    s = summarize(g)
    assert s["총DB"] == 3
    assert s["최종소진"] == 89400
    assert s["DB단가"] == round(89400 / 3)                 # 총소진÷총DB
    assert s["미전환소재소진"] == 12000


def test_zero_db_creatives_are_kept():
    """DB 0건 미전환 소재도 표에 남아야 한다."""
    ad = pd.DataFrame([ad_row("채무조정_ad6", "세트1", 51000),
                       ad_row("채무조정_ad7", "세트1", 0)])
    g = build_creative_table(ad, pd.DataFrame([db_row("kakaopay_ad1-6")]))
    assert set(g["소재"]) == {"채무조정_ad6", "채무조정_ad7"}


def test_duplicate_key_assigns_db_to_higher_spend():
    """채무조정_ad3 vs 카카오페이_ad3 처럼 같은 (세트1, 3) 키면 소진 큰 쪽만."""
    ad = pd.DataFrame([ad_row("채무조정_ad3", "세트1", 4800),
                       ad_row("카카오페이_ad3", "세트1", 0)])
    db = pd.DataFrame([db_row("kakaopay_ad1-3", "접수완료"),
                       db_row("kakaopay_ad1-3", "접수완료")])
    g = build_creative_table(ad, db).set_index("소재")
    assert g.loc["채무조정_ad3", "DB"] == 2
    assert g.loc["카카오페이_ad3", "DB"] == 0
    assert summarize(build_creative_table(ad, db))["총DB"] == 2   # 중복 계산 금지


def test_deduction_applies_to_creative_and_total():
    ad = pd.DataFrame([ad_row("채무조정_ad2", "세트1", 26400)])
    db = pd.DataFrame([db_row("kakaopay_ad1-2", "접수완료")])
    g = build_creative_table(ad, db, deduct={"채무조정_ad2": 6400})
    assert g.loc[0, "최종소진"] == 20000
    assert g.loc[0, "DB단가"] == 20000
    s = summarize(g)
    assert (s["총소진_차감전"], s["차감"], s["최종소진"]) == (26400, 6400, 20000)


def test_db_override_wins_over_sheet():
    ad = pd.DataFrame([ad_row("채무조정_ad6", "세트1", 50000)])
    db = pd.DataFrame([db_row("kakaopay_ad1-6", "접수완료")])
    g = build_creative_table(ad, db, db_override={"채무조정_ad6": 5})
    assert g.loc[0, "DB"] == 5
    assert g.loc[0, "DB단가"] == 10000


def test_test_set_is_excluded():
    ad = pd.DataFrame([ad_row("채무조정_ad6", "채무조정 세트 / 07~24 / 납입금 절감", 1000),
                       ad_row("카카오페이_ad1", "테스트 세트", 9999)])
    g = build_creative_table(ad, pd.DataFrame(columns=["날짜", "utm_source", "utm_campaign",
                                                      "utm_content", "접수", "승인", "구분"]))
    assert "카카오페이_ad1" not in set(g["소재"])
    assert summarize(g)["최종소진"] == 1000


def test_unmatched_db_listed():
    ad = pd.DataFrame([ad_row("채무조정_ad6", "세트1", 1000)])
    db = pd.DataFrame([db_row("kakaopay_ad1-6"), db_row("kakaopay_ad9-9")])
    g = build_creative_table(ad, db)
    um = unmatched_db(g, db)
    assert len(um) == 1 and um.loc[0, "utm_content"] == "kakaopay_ad9-9"


def test_parse_pairs():
    assert parse_pairs("채무조정_ad2 26400\n채무조정2_ad6: 1,200원\n쓰레기") == \
        {"채무조정_ad2": 26400.0, "채무조정2_ad6": 1200.0}


def test_report_text_shape():
    ad = pd.DataFrame([ad_row("채무조정_ad6", "채무조정 세트", 51000)])
    db = pd.DataFrame([db_row("kakaopay_ad1-6", "접수완료")])
    g = build_creative_table(ad, db)
    txt = report_text(g, summarize(g), D)
    assert "[소재별 DB 현황] — 2026-09-09" in txt
    assert "■ 채무조정 세트" in txt
    assert "- DB단가: 51,000원" in txt
    assert "[합계]" in txt


def test_db_columns_have_no_duplicates():
    """원본 상태값과 집계 플래그가 같은 이름을 쓰면 열이 조용히 덮어써진다."""
    from sources.db_sheet import BUCKETS, DB_COLUMNS
    assert len(DB_COLUMNS) == len(set(DB_COLUMNS)), f"중복 열: {DB_COLUMNS}"
    assert "접수상태" in DB_COLUMNS and "승인상태" in DB_COLUMNS
    for b in BUCKETS:
        assert b in DB_COLUMNS


def test_meeting_counts_in_both_meeting_and_received():
    """미팅확정 1건은 미팅에도, 접수에도 잡혀야 한다."""
    ad = pd.DataFrame([ad_row("채무조정_ad6", "세트1", 50000)])
    db = pd.DataFrame([db_row("kakaopay_ad1-6", "미팅 확정v2"),
                       db_row("kakaopay_ad1-6", "접수완료"),
                       db_row("kakaopay_ad1-6", "자산과다")])
    g = build_creative_table(ad, db)
    r = g.iloc[0]
    assert r["DB"] == 3
    assert r["미팅"] == 1
    assert r["접수"] == 2          # 미팅확정 + 접수완료
    assert r["진행불가"] == 1
    assert summarize(g)["접수이상"] == 2
