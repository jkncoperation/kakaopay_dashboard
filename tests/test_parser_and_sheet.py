"""광고센터 파일 파싱 + 시트 형식 변환 검증."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parsers.adcenter_file import AD_COLUMNS, ParseError, parse_adcenter_file  # noqa: E402
from sources.ad_sheet import (CLOSED_COLUMNS, SHEET_COLUMNS, TODAY_COLUMNS,  # noqa: E402
                              _from_sheet, _to_sheet)

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
    assert list(df.columns) == AD_COLUMNS            # 열 구성이 표준과 같다


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
    assert TODAY_COLUMNS == CLOSED_COLUMNS == SHEET_COLUMNS   # 두 탭 열 구성이 같다
    assert SHEET_COLUMNS[0] == "일자"
    assert "시작일" not in SHEET_COLUMNS and "종료일" not in SHEET_COLUMNS


def test_blank_date_becomes_given_default():
    """당일 탭: 다운로드 파일에는 날짜 열이 없어 그대로 붙여 넣으면 일자가 빈다."""
    rows = [["일자", "소재", "소진비용"], ["", "채무조정_ad6", "1,000"]]
    assert _from_sheet(rows).empty                       # 기본값 없으면 버림
    df = _from_sheet(rows, default_date=dt.date(2026, 9, 10))
    assert len(df) == 1 and df.loc[0, "날짜"] == dt.date(2026, 9, 10)

    no_date_col = [["소재", "소진비용"], ["채무조정_ad6", "1,000"]]
    df2 = _from_sheet(no_date_col, default_date=dt.date(2026, 9, 10))
    assert len(df2) == 1 and df2.loc[0, "소진비용"] == 1000


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


# ---------------------------------------------------------------- 시간대
def test_today_is_korea_time_not_server_time():
    """Streamlit Cloud 는 UTC 라, 한국 새벽에는 서버 날짜가 하루 뒤처진다.

    한국 01:00 = UTC 전날 16:00. 이때 date.today() 를 쓰면 '당일' 이 어제가 된다.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from core.util import today_kst

    # 한국 2026-09-10 01:22 == UTC 2026-09-09 16:22
    moment = dt.datetime(2026, 9, 9, 16, 22, tzinfo=dt.timezone.utc)
    assert moment.date() == dt.date(2026, 9, 9)                      # 서버(UTC) 기준
    assert moment.astimezone(ZoneInfo("Asia/Seoul")).date() == dt.date(2026, 9, 10)

    # 실제 함수는 항상 한국 날짜를 준다
    now_utc = dt.datetime.now(dt.timezone.utc)
    assert today_kst() == now_utc.astimezone(ZoneInfo("Asia/Seoul")).date()


def test_no_naive_today_in_source():
    """서버 시간대에 휘둘리는 date.today() 가 코드에 남아 있으면 안 된다."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for f in list(root.glob("*.py")) + list(root.glob("*/*.py")):
        if "test" in f.name or f.name == "util.py":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bdate\.today\(\)", line) and not line.strip().startswith("#"):
                offenders.append(f"{f.relative_to(root)}:{i}")
    assert not offenders, "한국 시간 대신 서버 시간을 쓰는 곳: " + ", ".join(offenders)
