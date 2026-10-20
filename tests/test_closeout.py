"""전일자 마감 타이밍 검증 — 하루 한 번, 01시 이후 첫 사이클에만."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collect_live import closeout_due  # noqa: E402


def at(h, m=0, day=21):
    return dt.datetime(2026, 9, day, h, m)


def test_before_the_hour_does_nothing():
    """자정~01시는 아직 전날이 안 끝난 것으로 본다."""
    assert closeout_due(at(0, 5), hour=1, done=None) is None
    assert closeout_due(at(0, 59), hour=1, done=None) is None


def test_first_cycle_after_the_hour_closes_yesterday():
    assert closeout_due(at(1, 0), hour=1, done=None) == dt.date(2026, 9, 20)
    assert closeout_due(at(1, 10), hour=1, done=None) == dt.date(2026, 9, 20)


def test_only_once_a_day():
    """이미 오늘 몫을 했으면 그날은 더 하지 않는다."""
    done = dt.date(2026, 9, 21)
    assert closeout_due(at(1, 10), hour=1, done=done) is None
    assert closeout_due(at(23, 59), hour=1, done=done) is None


def test_next_day_runs_again():
    done = dt.date(2026, 9, 21)
    assert closeout_due(at(1, 0, day=22), hour=1, done=done) == dt.date(2026, 9, 21)


def test_late_start_still_closes_out():
    """수집기가 꺼져 있다 낮에 켜져도 그날 몫을 한 번 한다."""
    assert closeout_due(at(14, 30), hour=1, done=None) == dt.date(2026, 9, 20)


def test_custom_hour():
    assert closeout_due(at(2, 0), hour=3, done=None) is None
    assert closeout_due(at(3, 0), hour=3, done=None) == dt.date(2026, 9, 20)


def test_실제_설정이_09시_마감이다():
    """01시에는 광고센터 수치가 아직 덜 들어온다 — 자정 직후로 넘어오는 집계가 있다.
    09시로 미뤄 전날이 확정된 뒤에 마감한다."""
    import json
    from pathlib import Path
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json")
                     .read_text(encoding="utf-8"))
    시각 = int(cfg.get("closeout_hour", 1))
    assert 시각 == 9
    assert closeout_due(at(8, 59), hour=시각, done=None) is None
    assert closeout_due(at(9, 0), hour=시각, done=None) == dt.date(2026, 9, 20)
    # 마감 전까지는 전날 데이터가 아직 마감 탭에 없다 — 하루 한 번은 지킨다
    assert closeout_due(at(14, 0), hour=시각, done=dt.date(2026, 9, 21)) is None
