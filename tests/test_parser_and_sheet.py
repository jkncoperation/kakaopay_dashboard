"""광고센터 파일 파싱 + 시트 형식 변환 검증."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parsers.adcenter_file import AD_COLUMNS, ParseError, parse_adcenter_file  # noqa: E402
from sources.ad_sheet import (CLOSED_COLUMNS, TODAY_COLUMNS, _from_sheet,  # noqa: E402
                              _to_sheet)

SAMPLE_0909 = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260909_20260909.xlsx"
SAMPLE_0906 = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260906_20260906.xlsx"
pytestmark = pytest.mark.filterwarnings("ignore")


# ---------------------------------------------------------------- 파일 파싱
@pytest.mark.skipif(not SAMPLE_0909.exists(), reason="샘플 없음")
def test_parse_real_file():
    r = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)
    df = r["df"]
    assert list(df.columns) == AD_COLUMNS
    assert str(r["start"]) == "2026-09-09"
    assert len(df) == 39
    assert df["소진비용"].sum() == 543640
    assert df["클릭수"].sum() == 1795
    assert 0 < df.loc[df["소재"] == "채무조정3_ad8", "클릭률"].iloc[0] < 0.01
    assert "채무조정 세트 / 07~13 / 연체" in set(df["광고그룹"])


@pytest.mark.skipif(not SAMPLE_0906.exists(), reason="샘플 없음")
def test_parse_other_date():
    r = parse_adcenter_file(str(SAMPLE_0906), filename=SAMPLE_0906.name)
    assert str(r["start"]) == "2026-09-06"
    assert len(r["df"]) == 8 and r["df"]["소진비용"].sum() == 502500


def test_parse_rejects_wrong_file(tmp_path):
    p = tmp_path / "아무거나.csv"
    p.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ParseError):
        parse_adcenter_file(str(p), filename=p.name)


def test_date_override_beats_filename():
    r = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name, date=dt.date(2026, 1, 1))
    assert set(r["df"]["날짜"]) == {dt.date(2026, 1, 1)}


# ---------------------------------------------------------------- 시트 형식
SHEET_ROWS = [
    ["일자", "소재", "ON/OFF", "상태", "광고그룹", "광고상품",
     "소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC"],
    ["2026-09-04", "채무조정_ad6", "ON", "진행중", "채무조정 세트 / 07~24 / 납입금 절감", "Fit 배너",
     "423,900", "345,173", "1,413", "0.41%", "178,684", "1,228", "300"],
    ["2026-09-04", "채무조정_ad7", "OFF", "진행중", "채무조정 세트 / 07~24 / 납입금 절감", "Fit 배너",
     "7,200", "10,891", "24", "0.22%", "6,624", "661", "300"],
]


def test_sheet_parses_comma_numbers_and_percent():
    """사람이 다운로드 파일을 붙여 넣으면 콤마·퍼센트 표기가 들어온다."""
    df = _from_sheet(SHEET_ROWS)
    assert len(df) == 2
    assert df.loc[0, "소진비용"] == 423900          # '423,900'
    assert df.loc[0, "노출수"] == 345173
    assert abs(df.loc[0, "클릭률"] - 0.0041) < 1e-9  # '0.41%' -> 비율
    assert df.loc[0, "출처"] == ""                   # 없는 열은 빈값


def test_sheet_accepts_both_date_headers():
    old = [["날짜", "소재", "소진비용"], ["2026-09-01", "채무조정_ad1", "1,000"]]
    df = _from_sheet(old)
    assert len(df) == 1 and df.loc[0, "소진비용"] == 1000


def test_sheet_roundtrip_keeps_values():
    df = _from_sheet(SHEET_ROWS)
    back = _from_sheet(_to_sheet(df, CLOSED_COLUMNS))
    assert back["소진비용"].sum() == df["소진비용"].sum()
    assert abs(back["클릭률"].sum() - df["클릭률"].sum()) < 1e-9


def test_sheet_headers():
    df = _from_sheet(SHEET_ROWS)
    assert _to_sheet(df, CLOSED_COLUMNS)[0] == CLOSED_COLUMNS
    assert _to_sheet(df, TODAY_COLUMNS)[0] == TODAY_COLUMNS
    assert CLOSED_COLUMNS[0] == "일자"               # 다운로드 파일과 같은 열 구성


def test_sheet_skips_blank_and_bad_rows():
    rows = SHEET_ROWS + [["", "", "", "", "", "", "", "", "", "", "", "", ""],
                         ["없는날짜", "채무조정_ad1", "", "", "", "", "1", "", "", "", "", "", ""]]
    assert len(_from_sheet(rows)) == 2


@pytest.mark.skipif(not SAMPLE_0909.exists(), reason="샘플 없음")
def test_uploaded_file_survives_sheet_roundtrip():
    """올린 파일 -> 시트 -> 다시 읽기 에서 숫자가 보존돼야 한다."""
    df = parse_adcenter_file(str(SAMPLE_0909), filename=SAMPLE_0909.name)["df"]
    back = _from_sheet(_to_sheet(df, CLOSED_COLUMNS))
    assert len(back) == len(df)
    assert back["소진비용"].sum() == df["소진비용"].sum()
    assert back["클릭수"].sum() == df["클릭수"].sum()
    assert set(back["소재"]) == set(df["소재"])


def test_empty_sheet():
    assert _from_sheet([]).empty
    assert _from_sheet([["일자", "소재"]]).empty
