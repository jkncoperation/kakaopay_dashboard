"""실제 광고센터 파일 + 합성 전환 데이터로 집계 전체를 검증."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.metrics import build_creative_table, summarize, unmatched_db  # noqa: E402
from parsers.adcenter_file import parse_adcenter_file  # noqa: E402
from sources.ad_sheet import CLOSED_COLUMNS, _from_sheet, _to_sheet  # noqa: E402
from sources.db_sheet import DB_COLUMNS, DBSheetError, _normalize  # noqa: E402


def _empty_db():
    return pd.DataFrame(columns=DB_COLUMNS)

SAMPLE = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260909_20260909.xlsx"
D = dt.date(2026, 9, 9)
pytestmark = [pytest.mark.skipif(not SAMPLE.exists(), reason="샘플 없음"),
              pytest.mark.filterwarnings("ignore")]


@pytest.fixture()
def data():
    """실제 09-09 파일을 시트에 올렸다가 다시 읽은 상태 + 그 소재에 맞는 전환 21건."""
    ad = parse_adcenter_file(str(SAMPLE), filename=SAMPLE.name)["df"]
    ad = _from_sheet(_to_sheet(ad, CLOSED_COLUMNS))      # 시트 왕복을 거친 데이터로 검증

    plan = [("kakaopay_ad1-6", 12), ("kakaopay_ad1-5", 2), ("kakaopay_ad2-2", 4),
            ("kakaopay_ad3-1", 2), ("kakaopay_ad9-9", 1)]   # 마지막은 매칭 실패용
    statuses = ["접수완료", "자산과다", "미팅확정", "", "소득미비"]
    rows = []
    for i, (content, n) in enumerate(plan):
        for j in range(n):
            rows.append({"신청 시간": "26.09.09 11:33:36", "utm_source": "kakaopay",
                         "utm_campaign": "kakaopay_adset", "utm_content": content,
                         "접수": statuses[(i + j) % len(statuses)],
                         "승인": "승인예정" if (i + j) % 7 == 0 else ""})
    rows.append({"신청 시간": "26.09.09 12:00:00", "utm_source": "naver", "utm_campaign": "x",
                 "utm_content": "naver_ad1-1", "접수": "접수완료", "승인": ""})
    return ad, _normalize(pd.DataFrame(rows))


def test_db_normalize_filters_and_classifies():
    raw = pd.DataFrame([
        {"신청 시간": "26.09.09 11:33:36", "utm_source": "kakaopay", "utm_campaign": "c",
         "utm_content": "kakaopay_ad1-6", "접수": "접수완료", "승인": ""},
        {"신청 시간": "Sep 9, 2026 11:41 AM", "utm_source": "kakaopay", "utm_campaign": "c",
         "utm_content": "kakaopay_ad2-2", "접수": "미팅확정", "승인": "승인완료"},
        {"신청 시간": "2026-09-09 13:00", "utm_source": "naver", "utm_campaign": "c",
         "utm_content": "naver_x", "접수": "접수완료", "승인": ""},
    ])
    d = _normalize(raw)
    assert len(d) == 2                      # naver 제외
    assert set(d["날짜"]) == {D}            # 세 가지 날짜 표기 모두 파싱
    assert list(d["구분"]) == ["접수", "승인"]   # 승인 열이 우선


def test_db_normalize_rejects_wrong_sheet():
    with pytest.raises(DBSheetError):
        _normalize(pd.DataFrame({"a": [1], "b": [2]}))


def test_full_pipeline(data):
    ad, db = data
    assert len(ad) == 39 and len(db) == 21

    g = build_creative_table(ad, db)
    assert not any("테스트" in x for x in g["광고그룹"])   # 테스트 세트 제외
    assert (g["전환수"] == 0).any()                            # 미전환 소재도 남는다

    s = summarize(g)
    assert s["총전환수"] == 20                                 # ad9-9 1건은 소재 없음
    assert s["지출"] == 543640
    assert s["전환단가"] == round(543640 / 20)
    assert len(unmatched_db(g, db)) == 1

    by = g.set_index("소재")
    assert by.loc["채무조정_ad6", "전환수"] == 12
    assert by.loc["채무조정_ad6", "전환단가"] == round(378600 / 12)
    assert by.loc["채무조정2_ad2", "전환수"] == 4
    assert by.loc["채무조정3_ad1", "전환수"] == 2


def test_period_merges_renamed_sets():
    """세트명이 바뀌어도 같은 소재가 두 줄로 갈라지지 않는다."""
    s0906 = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260906_20260906.xlsx"
    a1 = parse_adcenter_file(str(SAMPLE), filename=SAMPLE.name)["df"]
    a2 = parse_adcenter_file(str(s0906), filename=s0906.name)["df"]
    g = build_creative_table(pd.concat([a1, a2], ignore_index=True),
                             _empty_db())
    row = g[g["소재"] == "채무조정_ad6"]
    assert len(row) == 1
    assert row.iloc[0]["지출"] == 378600 + 437400
    assert row.iloc[0]["광고그룹"] == "채무조정 세트 / 07~24 / 납입금 절감"   # 가장 최근 세트명
