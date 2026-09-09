"""실제 광고센터 파일 + 합성 DB 로 파이프라인 전체(파일→저장→집계→리포트)를 검증."""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.metrics import build_creative_table, report_text, summarize, unmatched_db  # noqa: E402
from parsers.adcenter_file import parse_adcenter_file  # noqa: E402
from sources.db_sheet import DBSheetError, _normalize, status_bucket  # noqa: E402
from store import load_ad, load_db, save_db, upsert_ad  # noqa: E402

SAMPLE = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260909_20260909.xlsx"
D = dt.date(2026, 9, 9)
pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="샘플 파일 없음")


@pytest.fixture()
def seeded(tmp_path):
    """실제 09-09 파일을 저장소에 넣고, 그 소재에 맞는 DB 21건을 만들어 넣는다."""
    p = tmp_path / "t.sqlite"
    upsert_ad(parse_adcenter_file(str(SAMPLE), filename=SAMPLE.name)["df"], p)

    # 실제 소재 구성에 맞춘 utm_content (세트1=채무조정_, 2=채무조정2_, 3=채무조정3_)
    plan = [("kakaopay_ad1-6", 12), ("kakaopay_ad1-5", 2), ("kakaopay_ad2-2", 4),
            ("kakaopay_ad3-1", 2), ("kakaopay_ad9-9", 1)]        # 마지막 1건은 매칭 실패용
    statuses = ["접수완료", "자산과다", "미팅확정", "", "소득미비"]
    rows = []
    for i, (content, n) in enumerate(plan):
        for j in range(n):
            접수 = statuses[(i + j) % len(statuses)]
            승인 = "승인예정" if (i + j) % 7 == 0 else ""
            rows.append({"신청 시간": "26.09.09 11:33:36", "utm_source": "kakaopay",
                         "utm_campaign": "kakaopay_adset", "utm_content": content,
                         "접수": 접수, "승인": 승인})
    rows.append({"신청 시간": "26.09.09 12:00:00", "utm_source": "naver",     # 제외돼야 함
                 "utm_campaign": "x", "utm_content": "naver_ad1-1", "접수": "접수완료", "승인": ""})
    save_db(_normalize(pd.DataFrame(rows)), p)
    return p


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
    assert len(d) == 2                                  # naver 제외
    assert set(d["날짜"]) == {D}                        # 세 가지 날짜 표기 모두 파싱
    assert list(d["구분"]) == ["접수", "승인"]           # 승인 열이 우선


def test_db_normalize_rejects_wrong_sheet():
    with pytest.raises(DBSheetError):
        _normalize(pd.DataFrame({"a": [1], "b": [2]}))


def test_full_pipeline(seeded):
    ad = load_ad(D, D, seeded)
    db = load_db(D, D, seeded)
    assert len(ad) == 39 and len(db) == 21

    g = build_creative_table(ad, db)
    # 테스트 세트는 빠지고, DB 0건 소재도 남는다
    assert not any("테스트" in x for x in g["광고그룹"])
    assert (g["DB"] == 0).any()

    s = summarize(g)
    # 매칭된 20건만 잡히고(ad9-9 1건은 소재 없음), 소진은 테스트 세트 제외 합계
    assert s["총DB"] == 20
    assert s["최종소진"] == 543640
    assert s["DB단가"] == round(543640 / 20)
    assert len(unmatched_db(g, db)) == 1

    by = g.set_index("소재")
    assert by.loc["채무조정_ad6", "DB"] == 12
    assert by.loc["채무조정_ad6", "DB단가"] == round(378600 / 12)
    assert by.loc["채무조정2_ad2", "DB"] == 4
    assert by.loc["채무조정3_ad1", "DB"] == 2
    # 각 구분 합이 DB 총계와 맞아야 한다
    assert int(g[["진행불가", "접수", "미팅", "승인"]].sum().sum()) + \
        int((g["DB"] - g[["진행불가", "접수", "미팅", "승인"]].sum(axis=1)).sum()) == 20


def test_report_matches_summary(seeded):
    ad, db = load_ad(D, D, seeded), load_db(D, D, seeded)
    g = build_creative_table(ad, db, deduct={"채무조정_ad6": 8600})
    s = summarize(g)
    txt = report_text(g, s, D, D, {"채무조정_ad6": 8600})
    assert f"- 최종 소진: {s['최종소진']:,.0f}원" in txt
    assert f"- 총 DB: {s['총DB']}건" in txt
    assert f"- 최종 DB단가: {s['DB단가']:,}원" in txt
    assert "  · 채무조정_ad6: 8,600원" in txt
    assert s["최종소진"] == 543640 - 8600


def test_period_query_spans_days(tmp_path):
    p = tmp_path / "t.sqlite"
    s0906 = ROOT / "samples" / "(카카오페이)법무법인 평온_소재_20260906_20260906.xlsx"
    upsert_ad(parse_adcenter_file(str(SAMPLE), filename=SAMPLE.name)["df"], p)
    upsert_ad(parse_adcenter_file(str(s0906), filename=s0906.name)["df"], p)
    g = build_creative_table(load_ad("2026-09-01", "2026-09-30", p),
                             pd.DataFrame(columns=["날짜", "utm_source", "utm_campaign",
                                                   "utm_content", "접수", "승인", "구분"]))
    # 같은 소재가 두 날짜에 있으면 기간 합산으로 한 행이 된다
    assert summarize(g)["최종소진"] == 543640 + 502500
    row = g[g["소재"] == "채무조정_ad6"]
    assert len(row) == 1
    assert row.iloc[0]["소진"] == 378600 + 437400
    # 세트명이 바뀌어도 갈라지지 않고, 가장 최근 세트명을 쓴다
    assert row.iloc[0]["광고그룹"] == "채무조정 세트 / 07~24 / 납입금 절감"
