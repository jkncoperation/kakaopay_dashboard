"""당일 실시간 수집기 테스트 - 브라우저 없이 도는 것만 (표 파싱 · 열 매핑 · URL).

실제 광고센터 화면(2026-09-17)에서 읽은 값을 그대로 넣어, 표준 스키마로 잘 바뀌는지 본다.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.adcenter_file import AD_COLUMNS          # noqa: E402
from sources import adcenter_live as live             # noqa: E402

DAY = dt.date(2026, 9, 17)
GROUP = "채무조정 세트1 / 납입금 절감 / 커스텀타겟 / 야간"

# 소재 탭 실제 헤더
CONTENT_HEAD = ["", "소재 ID", "소재", "ON/OFF", "상태", "상위 광고그룹 이름", "광고 상품",
                "소진 비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC", "기간"]


def crow(cid, name, spend, imp, clk, ctr, reach, ecpm, cpc, status="진행중"):
    return ["", cid, name, "", status, GROUP, "Fit 배너", spend, imp, clk, ctr, reach, ecpm, cpc,
            "2026-09-03 ~ 2026-12-31"]


CONTENT = {"head": CONTENT_HEAD, "rows": [
    {"on": True, "t": crow("10008637", "채무조정_ad6", "55,800원", "62,472", "186", "0.3%", "33,625", "893원", "300원")},
    {"on": False, "t": crow("10008638", "채무조정_ad7", "0원", "0", "0", "0%", "0", "0원", "0원")},
    {"on": None, "t": crow("10008593", "카카오페이_ad3", "0원", "0", "0", "0%", "0", "0원", "0원", status="심사 반려")},
]}

# 광고그룹 탭 실제 헤더
GROUP_HEAD = ["", "광고그룹 ID", "광고그룹", "ON/OFF", "상태", "상위 캠페인 이름", "광고상품", "입찰가",
              "광고그룹 예산", "일 예산", "소진 비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC", "기간"]


# ---------------------------------------------------------------- 소재 표 → 표준 스키마
def test_표준_스키마로_바뀐다():
    df = live.rows_to_frame(CONTENT, DAY)
    assert list(df.columns) == AD_COLUMNS
    assert len(df) == 3
    r = df.iloc[0]
    assert r["날짜"] == DAY and r["소재"] == "채무조정_ad6" and r["ON/OFF"] == "ON"
    assert r["상태"] == "진행중" and r["광고그룹"] == GROUP and r["광고상품"] == "Fit 배너"
    assert r["소진비용"] == "55,800" and r["노출수"] == "62,472" and r["클릭률"] == "0.3%"
    assert r["eCPM"] == "893" and r["CPC"] == "300"


def test_대시보드_파서가_그대로_읽는다():
    """시트에 쓴 뒤 대시보드가 읽어 들이는 경로까지 확인 (숫자·퍼센트 표기)."""
    from sources.ad_sheet import _from_sheet, SHEET_COLUMNS
    df = live.rows_to_frame(CONTENT, DAY)
    vals = [SHEET_COLUMNS] + df.astype(str).values.tolist()
    back = _from_sheet(vals)
    assert len(back) == 3
    assert back.iloc[0]["소진비용"] == 55800.0
    assert back.iloc[0]["클릭률"] == pytest.approx(0.003)     # '0.3%' → 비율
    assert back["소진비용"].sum() == 55800.0


def test_ONOFF_토글_상태():
    df = live.rows_to_frame(CONTENT, DAY)
    assert list(df["ON/OFF"]) == ["ON", "OFF", ""]     # 못 읽으면 빈칸


def test_원표기는_떼고_퍼센트는_남긴다():
    assert live._clean("55,800원") == "55,800"
    assert live._clean("0.3%") == "0.3%"
    assert live._clean("") == ""


def test_칸이_모자란_줄은_건너뛴다():
    data = {"head": CONTENT_HEAD, "rows": CONTENT["rows"] + [{"on": None, "t": ["합계"]}]}
    assert len(live.rows_to_frame(data, DAY)) == 3


def test_소재가_없으면_빈_표():
    df = live.rows_to_frame({"head": CONTENT_HEAD, "rows": []}, DAY)
    assert df.empty and list(df.columns) == AD_COLUMNS


# ---------------------------------------------------------------- 열 매핑
def test_헤더_이름으로_열을_찾는다():
    idx, fell_back = live.map_columns(CONTENT_HEAD, 15, live.CONTENT_COLS, live.CONTENT_FALLBACK)
    assert not fell_back
    assert idx["소재"] == 2 and idx["소진비용"] == 7 and idx["CPC"] == 13


def test_열_순서가_바뀌어도_따라간다():
    head = list(CONTENT_HEAD)
    head[7], head[10] = head[10], head[7]
    rows = []
    for r in CONTENT["rows"]:
        t = list(r["t"])
        t[7], t[10] = t[10], t[7]
        rows.append({"on": r["on"], "t": t})
    df = live.rows_to_frame({"head": head, "rows": rows}, DAY)
    assert df.iloc[0]["소진비용"] == "55,800" and df.iloc[0]["클릭률"] == "0.3%"


def test_헤더를_못_읽으면_기본_순서():
    idx, fell_back = live.map_columns([], 15, live.CONTENT_COLS, live.CONTENT_FALLBACK)
    assert idx == live.CONTENT_FALLBACK and set(fell_back) == set(live.CONTENT_FALLBACK)


def test_체크박스_열에_th_가_없어도_맞춘다():
    idx, _ = live.map_columns(CONTENT_HEAD[1:], 15, live.CONTENT_COLS, live.CONTENT_FALLBACK)
    assert idx["소재"] == 2 and idx["소진비용"] == 7


def test_광고그룹_표에서는_소진만_있으면_된다():
    idx, fell_back = live.map_columns(GROUP_HEAD, 18, live.GROUP_COLS, live.GROUP_FALLBACK)
    assert not fell_back
    assert idx["id"] == 1 and idx["name"] == 2 and idx["spend"] == 10
    # '광고그룹 예산' 이 '광고그룹' 으로 잘못 잡히면 안 된다
    assert idx["name"] != 8


# ---------------------------------------------------------------- URL
CFG = live.collector_config({"ad_account_id": "10000246"})


def test_광고그룹_목록에는_캠페인_필터를_안_붙인다():
    """캠페인을 걸면 다른 캠페인의 그룹이 빠진다. 미선택 상태여야 전부 보인다."""
    u = live.group_list_url(CFG, DAY)
    assert "campaignId" not in u
    assert "/group?startDate=2026-09-17&endDate=2026-09-17&size=100" in u


def test_고른_그룹이_adGroupId_에_콤마로_붙는다():
    """화면에서 좌측 체크박스를 켜고 '소재' 탭을 누르면 광고센터가 만드는 URL 과 같은 형태."""
    u = live.content_url(CFG, DAY, ["10002189", "10002280"])
    assert u.endswith("&adGroupId=10002189,10002280")


def test_그룹을_안_고르면_필터가_없다():
    assert "adGroupId" not in live.content_url(CFG, DAY, [])


def test_설정_기본값과_덮어쓰기():
    assert live.collector_config({})["interval_min"] == 10
    c = live.collector_config({"collector": {"interval_min": 30, "min_spend": 1000}})
    assert c["interval_min"] == 30 and c["min_spend"] == 1000
    assert c["ad_account_id"] == live.DEFAULTS["ad_account_id"]


# ---------------------------------------------------------------- 갱신 시각 정렬
def test_시계에_맞춰_다음_슬롯을_고른다():
    """10분 간격이면 :00 :10 :20 … 에 갱신한다 (시작 시각 기준이 아니라)."""
    import collect_live

    def wait_at(h, m, s, interval=10):
        return collect_live.seconds_to_next_slot(interval, dt.datetime(2026, 9, 17, h, m, s))

    assert wait_at(3, 3, 0) == 7 * 60          # 03:03 → 03:10
    assert wait_at(3, 13, 0) == 7 * 60         # 03:13 → 03:20
    assert wait_at(3, 29, 30) == 30            # 03:29:30 → 03:30
    assert wait_at(3, 50, 0) == 10 * 60        # 03:50 → 04:00
    assert wait_at(3, 59, 59) == 1             # 자정·정시 넘김도 안전


def test_다른_간격도_시계에_맞춘다():
    import collect_live
    assert collect_live.seconds_to_next_slot(30, dt.datetime(2026, 9, 17, 3, 5)) == 25 * 60
    assert collect_live.seconds_to_next_slot(5, dt.datetime(2026, 9, 17, 3, 7)) == 3 * 60
    assert collect_live.seconds_to_next_slot(1, dt.datetime(2026, 9, 17, 3, 7, 20)) == 40


def test_정렬을_끌_수도_있다():
    assert live.collector_config({})["align_to_clock"] is True
    assert live.collector_config({"collector": {"align_to_clock": False}})["align_to_clock"] is False
