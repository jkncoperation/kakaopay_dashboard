"""파서(실제 샘플 파일) + 로컬 저장소 검증."""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parsers.adcenter_file import AD_COLUMNS, ParseError, parse_adcenter_file  # noqa: E402
from store import available_dates, delete_date, load_ad, upsert_ad  # noqa: E402

SAMPLE_0909 = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260909_20260909.xlsx"
SAMPLE_0906 = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260906_20260906.xlsx"


@pytest.mark.skipif(not SAMPLE_0909.exists(), reason="샘플 파일 없음")
def test_parse_real_file():
    r = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)
    df = r["df"]
    assert list(df.columns) == AD_COLUMNS
    assert str(r["start"]) == "2026-09-09" and str(r["end"]) == "2026-09-09"
    assert len(df) == 39
    assert df["소진비용"].sum() == 543640
    assert df["클릭수"].sum() == 1795
    # 클릭률은 비율(0~1)로 정규화
    assert 0 < df.loc[df["소재"] == "채무조정3_ad8", "클릭률"].iloc[0] < 0.01
    # 세트명·소재명이 그대로 들어옴
    assert "채무조정 세트 / 07~13 / 연체" in set(df["광고그룹"])
    assert "채무조정_ad6" in set(df["소재"])


@pytest.mark.skipif(not SAMPLE_0906.exists(), reason="샘플 파일 없음")
def test_parse_other_date_file():
    r = parse_adcenter_file(str(SAMPLE_0906), filename=SAMPLE_0906.name)
    assert str(r["start"]) == "2026-09-06"
    assert len(r["df"]) == 8
    assert r["df"]["소진비용"].sum() == 502500


def test_parse_rejects_wrong_file(tmp_path):
    p = tmp_path / "아무거나.csv"
    p.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ParseError):
        parse_adcenter_file(str(p), filename=p.name)


def test_date_override_beats_filename():
    import datetime as dt
    r = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name, date=dt.date(2026, 1, 1))
    assert str(r["start"]) == "2026-01-01"
    assert set(r["df"]["날짜"]) == {dt.date(2026, 1, 1)}


# ---------------------------------------------------------------- 저장소
@pytest.fixture()
def store_path(tmp_path):
    return tmp_path / "t.sqlite"


def test_upsert_and_load(store_path):
    r = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)
    n = upsert_ad(r["df"], store_path)
    assert n == 39
    got = load_ad(path=store_path)
    assert len(got) == 39
    assert got["소진비용"].sum() == 543640
    assert available_dates(store_path) == ["2026-09-09"]


def test_upsert_same_date_overwrites(store_path):
    r = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)
    upsert_ad(r["df"], store_path)
    # 같은 날짜를 값만 바꿔 다시 넣으면 행이 늘지 않고 값이 갱신돼야 함
    df2 = r["df"].copy()
    df2["소진비용"] = 1.0
    upsert_ad(df2, store_path)
    got = load_ad(path=store_path)
    assert len(got) == 39
    assert got["소진비용"].sum() == 39


def test_two_dates_coexist_and_range_query(store_path):
    upsert_ad(parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)["df"], store_path)
    upsert_ad(parse_adcenter_file(str(SAMPLE_0906), filename=SAMPLE_0906.name)["df"], store_path)
    assert sorted(available_dates(store_path)) == ["2026-09-06", "2026-09-09"]
    assert len(load_ad("2026-09-09", "2026-09-09", store_path)) == 39
    assert len(load_ad("2026-09-06", "2026-09-06", store_path)) == 8
    assert len(load_ad("2026-09-01", "2026-09-30", store_path)) == 47


def test_delete_date(store_path):
    upsert_ad(parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)["df"], store_path)
    assert delete_date("2026-09-09", store_path) == 39
    assert load_ad(path=store_path).empty


def test_upsert_empty_is_noop(store_path):
    assert upsert_ad(pd.DataFrame(), store_path) == 0


# ---------------------------------------------------------------- 실시간(화면 읽기) 경로
def test_live_table_to_df_and_normalize():
    """화면에서 읽은 표도 파일과 같은 표준 스키마로 정규화돼야 한다."""
    import datetime as dt

    from collect_live import table_to_df
    from parsers.adcenter_file import normalize_raw

    head = ["소재", "ON/OFF", "상태", "광고그룹", "광고상품", "소진비용", "노출수",
            "클릭수", "클릭률", "도달수", "eCPM", "CPC", "기간"]
    rows = [  # 맨 앞은 행 선택 체크박스 칸 → td 가 헤더보다 1개 많음
        {"tds": ["", "채무조정3_ad8", "", "진행중", "채무조정 세트 / 13 ~ 19 / 연체", "Fit 배너",
                 "17,100원", "51,980", "57", "0.11%", "25,499", "329", "300",
                 "2026-09-08 ~ 2026-12-31"], "onoff": "ON"},
        {"tds": ["", "채무조정3_ad5", "", "심사 반려", "채무조정 세트 / 13 ~ 19 / 연체", "Fit 배너",
                 "0원", "0", "0", "0%", "0", "0", "0",
                 "2026-09-08 ~ 2026-12-31"], "onoff": "OFF"},
    ]
    res = normalize_raw(table_to_df(head, rows), date=dt.date(2026, 9, 9), 출처="live")
    df = res["df"]
    assert list(df.columns) == AD_COLUMNS
    assert len(df) == 2
    assert df.loc[0, "소재"] == "채무조정3_ad8"
    assert df.loc[0, "ON/OFF"] == "ON" and df.loc[1, "ON/OFF"] == "OFF"
    assert df.loc[0, "소진비용"] == 17100          # '17,100원' → 숫자
    assert abs(df.loc[0, "클릭률"] - 0.0011) < 1e-9   # '0.11%' → 비율
    assert df.loc[0, "출처"] == "live"
    assert set(df["날짜"]) == {dt.date(2026, 9, 9)}


def test_live_table_without_checkbox_column():
    """체크박스 칸이 없는 표(td 개수 == 헤더 개수)도 그대로 읽혀야 한다."""
    import datetime as dt

    from collect_live import table_to_df
    from parsers.adcenter_file import normalize_raw

    head = ["소재", "상태", "광고그룹", "소진비용", "노출수", "클릭수"]
    rows = [{"tds": ["채무조정_ad6", "진행중", "채무조정 세트", "378,600원", "546,662", "1,262"],
             "onoff": "ON"}]
    df = normalize_raw(table_to_df(head, rows), date=dt.date(2026, 9, 9), 출처="live")["df"]
    assert df.loc[0, "소재"] == "채무조정_ad6"
    assert df.loc[0, "소진비용"] == 378600
    assert df.loc[0, "클릭수"] == 1262
