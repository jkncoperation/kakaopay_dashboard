"""화면 표 세 개(소재별·세트별·일별)의 열 구성과 합계 검증."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.metrics import build_creative_table, summarize  # noqa: E402
from sources.db_sheet import DB_COLUMNS, status_bucket, status_flags  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")
D1, D2 = dt.date(2026, 9, 8), dt.date(2026, 9, 9)


@pytest.fixture(scope="module")
def app():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kpapp", ROOT / "app.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ad_row(날짜, 소재, 광고그룹, 지출, 노출=10000, 클릭=30):
    return {"날짜": 날짜, "소재": 소재, "ON/OFF": "ON", "상태": "진행중", "광고그룹": 광고그룹,
            "광고상품": "Fit 배너", "소진비용": 지출, "노출수": 노출, "클릭수": 클릭,
            "클릭률": 0.0, "도달수": 0.0, "eCPM": 0.0, "CPC": 0.0}


def db_row(날짜, content, 접수="", 승인=""):
    return {"날짜": 날짜, "utm_source": "kakaopay", "utm_campaign": "c",
            "utm_content": content, "접수상태": 접수, "승인상태": 승인,
            "구분": status_bucket(접수, 승인), **status_flags(접수, 승인)}


@pytest.fixture()
def data():
    ad = pd.DataFrame([ad_row(D1, "채무조정_ad6", "세트1", 100000),
                       ad_row(D1, "채무조정_ad2", "세트1", 50000),
                       ad_row(D2, "채무조정_ad6", "세트1", 200000),
                       ad_row(D2, "채무조정2_ad1", "세트2", 60000)])
    db = pd.DataFrame([db_row(D1, "kakaopay_ad1-6", "접수완료"),
                       db_row(D1, "kakaopay_ad1-6", "미팅확정"),
                       db_row(D1, "kakaopay_ad1-2", "자산과다"),
                       db_row(D2, "kakaopay_ad1-6", "미팅확정", "승인완료"),
                       db_row(D2, "kakaopay_ad2-1", "접수완료")])
    return ad, db


def test_three_tables_share_the_same_columns(app, data):
    """앞쪽 이름칸만 다르고 지표 열은 모두 같아야 한다."""
    ad, db = data
    g = build_creative_table(ad, db)
    ct = app.creative_table(g)
    gt = app.group_table(g)
    dt_ = app.daily_table(ad, db, {}, ("테스트",))

    metrics = ["지출", "전환수", "전환단가", "진행불가", "접수", "미팅", "승인",
               "CPM", "CTR", "CPC", "제출율"]
    assert list(ct.columns) == ["광고그룹", "소재", "ON/OFF", "상태", *metrics]
    assert list(gt.columns) == ["광고그룹", *metrics]
    assert list(dt_.columns) == ["날짜", *metrics]


def test_daily_table_has_totals_row(app, data):
    """일별 표 맨 아래 합계가 전체 집계와 같아야 한다."""
    ad, db = data
    dt_ = app.daily_table(ad, db, {}, ("테스트",))
    assert list(dt_["날짜"])[:-1] == [str(D1), str(D2)]
    assert dt_.iloc[-1]["날짜"] == "합계"

    s = summarize(build_creative_table(ad, db))
    total = dt_.iloc[-1]
    assert total["지출"] == s["지출"] == 410000
    assert total["전환수"] == s["총전환수"] == 5
    assert total["전환단가"] == f"{round(410000 / 5):,}"


def test_daily_rows_sum_to_total(app, data):
    ad, db = data
    dt_ = app.daily_table(ad, db, {}, ("테스트",))
    rows, total = dt_.iloc[:-1], dt_.iloc[-1]
    assert rows["지출"].sum() == total["지출"]
    assert rows["전환수"].sum() == total["전환수"]


def test_group_table_sums_to_same_total(app, data):
    ad, db = data
    g = build_creative_table(ad, db)
    gt = app.group_table(g)
    assert gt["지출"].sum() == summarize(g)["지출"]
    assert gt["전환수"].sum() == summarize(g)["총전환수"]


def test_empty_daily_table(app):
    empty_ad = pd.DataFrame(columns=["날짜", "소재", "ON/OFF", "상태", "광고그룹", "광고상품",
                                     "소진비용", "노출수", "클릭수", "클릭률", "도달수",
                                     "eCPM", "CPC"])
    out = app.daily_table(empty_ad, pd.DataFrame(columns=DB_COLUMNS), {}, ("테스트",))
    assert out.empty
