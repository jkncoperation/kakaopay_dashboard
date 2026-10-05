"""시트 접속 캐시 — 같은 자격 증명이면 한 번만 열고, 실패하면 캐시를 버리고 다시 시도."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sources import gsheets  # noqa: E402

SCOPES = ["s"]


@pytest.fixture(autouse=True)
def clean():
    gsheets._CLIENTS.clear()
    gsheets.invalidate()
    yield
    gsheets._CLIENTS.clear()
    gsheets.invalidate()


class FakeWS:
    def __init__(self, title): self.title = title


class FakeSheet:
    def __init__(self, counter): self.counter = counter
    def worksheet(self, t):
        self.counter["ws"] += 1
        if t == "없는탭":
            raise RuntimeError("404")
        return FakeWS(t)
    def get_worksheet(self, i):
        self.counter["ws"] += 1
        return FakeWS(f"#{i}")


def fake_client(counter):
    class C:
        def open_by_key(self, sid):
            counter["open"] += 1
            return FakeSheet(counter)
    return C()


def test_spreadsheet_opened_once(monkeypatch):
    """open_by_key 가 1.4초라 반복 호출을 막는 것이 이 캐시의 목적."""
    counter = {"open": 0, "ws": 0}
    monkeypatch.setattr(gsheets, "client", lambda *a, **k: fake_client(counter))
    for _ in range(3):
        gsheets.worksheet("SHEET", "탭A", None, "key.json", SCOPES)
    assert counter["open"] == 1          # 스프레드시트는 한 번만 열린다
    assert counter["ws"] == 1            # 탭 핸들도 한 번만


def test_different_tabs_share_one_open(monkeypatch):
    counter = {"open": 0, "ws": 0}
    monkeypatch.setattr(gsheets, "client", lambda *a, **k: fake_client(counter))
    gsheets.worksheet("SHEET", "탭A", None, "key.json", SCOPES)
    gsheets.worksheet("SHEET", "탭B", None, "key.json", SCOPES)
    assert counter["open"] == 1          # 당일·마감 두 탭이 한 번의 열기를 공유
    assert counter["ws"] == 2


def test_invalidate_reopens(monkeypatch):
    counter = {"open": 0, "ws": 0}
    monkeypatch.setattr(gsheets, "client", lambda *a, **k: fake_client(counter))
    gsheets.worksheet("SHEET", "탭A", None, "key.json", SCOPES)
    gsheets.invalidate()
    gsheets.worksheet("SHEET", "탭A", None, "key.json", SCOPES)
    assert counter["open"] == 2


def test_with_retry_clears_cache_and_retries():
    """탭이 삭제·재생성되면 캐시된 핸들이 낡는다. 조용히 틀리지 않고 다시 열어야 한다."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("stale handle")
        return "ok"

    assert gsheets.with_retry(flaky) == "ok"
    assert calls["n"] == 2


def test_with_retry_reraises_if_still_failing():
    def always_bad():
        raise RuntimeError("no access")

    with pytest.raises(RuntimeError):
        gsheets.with_retry(always_bad)


class FakeGC:
    """gspread.Client 대역. set_timeout 을 받아 기록한다."""
    def __init__(self):
        self.timeout = None

    def set_timeout(self, t):
        self.timeout = t


def patch_auth(monkeypatch) -> list:
    """authorize/Credentials 를 가짜로 바꾸고, 만들어진 클라이언트 목록을 돌려준다."""
    made = []

    def fake_authorize(creds):
        made.append(FakeGC())
        return made[-1]

    monkeypatch.setattr(gsheets.gspread, "authorize", fake_authorize)
    monkeypatch.setattr(gsheets, "_CLIENTS", {})

    class FakeCreds:
        @staticmethod
        def from_service_account_info(info, scopes=None): return "creds-info"

    import google.oauth2.service_account as sa
    monkeypatch.setattr(sa, "Credentials", FakeCreds)
    return made


def test_client_cached_per_credentials(monkeypatch):
    made = patch_auth(monkeypatch)
    info = {"client_email": "a@b.iam.gserviceaccount.com"}
    gsheets.client(info, "k.json", SCOPES)
    gsheets.client(info, "k.json", SCOPES)
    assert len(made) == 1                # 같은 계정이면 인증 한 번

# ---------------------------------------------------------------- timeout
def test_클라이언트에_timeout이_걸린다(monkeypatch):
    """timeout 이 None 이면 응답 없는 요청을 영원히 기다려 '무한로딩' 이 된다."""
    made = patch_auth(monkeypatch)
    gc = gsheets.client({"client_email": "x@y.z"}, "none.json", SCOPES)
    assert gc.timeout == gsheets.TIMEOUT
    assert made[0].timeout is not None


def test_timeout_값이_유한하다():
    """(연결, 읽기) 둘 다 양수여야 한다."""
    t = gsheets.TIMEOUT
    assert isinstance(t, tuple) and len(t) == 2
    assert all(isinstance(x, (int, float)) and x > 0 for x in t)


def test_set_timeout이_실제_gspread에_있다():
    """gspread 버전이 올라가 API 가 바뀌면 timeout 이 조용히 안 걸릴 수 있다."""
    import gspread
    assert hasattr(gspread.Client, "set_timeout")
