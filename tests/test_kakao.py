"""카카오모먼트 연동 - 소재명 매칭, 당일/기간 분할 호출, 집계.

실제 API 는 부르지 않는다. 호출 제한이 운영(비즈폼 동기화)과 공유되기 때문이다.
대신 실제 응답 모양과 실제 이름을 그대로 본뜬 가짜를 쓴다.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sources import kakao_api, meta_table  # noqa: E402


# ---------------------------------------------------------------- 소재명 매칭
# 실제로 겪은 어긋남: 소재명을 고쳐도 폼에 박힌 utm_content 는 옛 이름으로 남는다.
@pytest.mark.parametrize("소재명, utm_content", [
    ("법무법인 평온 - kakao8_1029x258_메인", "법무법인 평온 - kakao8_1029x258"),
    ("법무법인 평온 - kakao13_1029x258 / 메인", "법무법인 평온 - kakao13_1029x258"),
    ("법무법인 평온 - kakao1_1029x258_메인", "법무법인 평온 - kakao1_1029x258"),
    ("법무법인 평온 - kakao5_1029x258", "법무법인 평온 - kakao5_1029x258"),
])
def test_꼬리표를_떼면_전환이_붙는다(소재명, utm_content):
    assert kakao_api.match_key(소재명) == kakao_api.match_key(utm_content)


@pytest.mark.parametrize("이름", [
    "[비즈니스폼+] new kakao123_1029x258 / 간편양식",
    "[비즈니스폼+] new kakao120_1029x258 / 간편양식",
])
def test_간편양식_꼬리표는_떼지_않는다(이름):
    """utm_content 에도 그대로 들어 있는 꼬리표다. 떼면 서로 다른 소재가 한 키로 합쳐진다."""
    assert kakao_api.match_key(이름) == 이름.lower()


def test_다른_소재가_한_키로_합쳐지면_알려준다():
    충돌 = kakao_api.key_collisions(
        ["소재A_메인", "소재A", "[비즈니스폼+] new kakao3_1029x258"])
    assert "소재a" in 충돌
    assert 충돌["소재a"] == ["소재A", "소재A_메인"]
    assert len(충돌) == 1          # 겹치지 않는 이름은 걸리지 않는다


# ---------------------------------------------------------------- 당일/기간 분할
class FakeGet:
    """_get 대역. 어떤 파라미터로 불렀는지 기록한다."""

    def __init__(self, per_day=1000.0):
        self.calls: list[dict] = []
        self.per_day = per_day

    def __call__(self, path, account, token, timeout=40, **params):
        self.calls.append({"path": path, **params})
        start, end = params.get("start"), params.get("end")
        if start and end:
            d0 = dt.datetime.strptime(start, "%Y%m%d").date()
            d1 = dt.datetime.strptime(end, "%Y%m%d").date()
            days = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
        else:
            days = [kakao_api.today_kst()]          # 기간을 안 주면 오늘
        return {"data": [{"start": d.isoformat(), "end": d.isoformat(),
                          "dimensions": {"creative_id": "1"},
                          "metrics": {"imp": 100, "click": 5, "cost": self.per_day}}
                         for d in days]}


@pytest.fixture
def fake(monkeypatch):
    f = FakeGet()
    monkeypatch.setattr(kakao_api, "_get", f)
    return f


def test_당일은_기간없이_부른다(fake):
    """start/end 에 오늘이 들어가면 API 가 기간을 거부한다 (detailCode 60008)."""
    T = kakao_api.today_kst()
    kakao_api.report("creatives", 1, 9, "tok", T, T)
    assert len(fake.calls) == 1
    assert "start" not in fake.calls[0] and "end" not in fake.calls[0]


def test_지난_기간은_기간을_주고_부른다(fake):
    T = kakao_api.today_kst()
    kakao_api.report("creatives", 1, 9, "tok", T - dt.timedelta(5), T - dt.timedelta(1))
    assert len(fake.calls) == 1
    assert fake.calls[0]["start"] == (T - dt.timedelta(5)).strftime("%Y%m%d")
    assert fake.calls[0]["end"] == (T - dt.timedelta(1)).strftime("%Y%m%d")


def test_오늘이_걸친_기간은_두_번_나눠_부른다(fake):
    """한 번에 부르면 기간 전체가 거부되어 그 구간이 통째로 빈다."""
    T = kakao_api.today_kst()
    rows = kakao_api.report("creatives", 1, 9, "tok", T - dt.timedelta(2), T)
    assert len(fake.calls) == 2
    기간조회 = [c for c in fake.calls if "start" in c]
    당일조회 = [c for c in fake.calls if "start" not in c]
    assert len(기간조회) == 1 and len(당일조회) == 1
    # 기간 조회는 어제까지만 (오늘이 섞이면 거부당한다)
    assert 기간조회[0]["end"] == (T - dt.timedelta(1)).strftime("%Y%m%d")
    날짜 = sorted(r["start"] for r in rows)
    assert 날짜 == sorted((T - dt.timedelta(i)).isoformat() for i in (2, 1, 0))


def test_미래_날짜만_있으면_부르지_않는다(fake):
    T = kakao_api.today_kst()
    rows = kakao_api.report("creatives", 1, 9, "tok", T + dt.timedelta(1), T + dt.timedelta(2))
    assert fake.calls == [] and rows == []


def test_리포트_종류별_파라미터_이름():
    assert kakao_api.ID_PARAM == {"campaigns": "campaignId",
                                  "adGroups": "adGroupId",
                                  "creatives": "creativeId"}


# ---------------------------------------------------------------- fetch 범위
def _structure(monkeypatch, campaigns):
    """campaigns/adGroups/creatives 를 가짜로 바꾼다. 세트·소재는 각 1개."""
    monkeypatch.setattr(kakao_api, "campaigns", lambda a, t: campaigns)
    monkeypatch.setattr(kakao_api, "ad_groups",
                        lambda a, t, cid: [{"id": cid * 10, "name": f"세트{cid}"}])
    monkeypatch.setattr(kakao_api, "creatives",
                        lambda a, t, gid: [{"id": gid * 10, "name": f"소재{gid}"}])
    monkeypatch.setattr(kakao_api, "credentials", lambda token=None: "tok")


def test_기본은_켜진_캠페인만_본다(monkeypatch, fake):
    """계정을 전부 훑으면 112회 호출에 693초가 걸렸다. 기본값은 ON 만."""
    _structure(monkeypatch, [{"id": 1, "name": "켜짐", "config": "ON"},
                             {"id": 2, "name": "꺼짐", "config": "OFF"}])
    T = kakao_api.today_kst()
    kakao_api.forget_spenders()
    ads = kakao_api.fetch(9, T - dt.timedelta(1), T - dt.timedelta(1))
    assert set(ads["캠페인"]) == {"켜짐"}
    # 기억이 없는 첫 조회는 세트 지출로 범위를 좁힌다 (캠페인 리포트는 쓰지 않는다)
    assert [c["path"] for c in fake.calls].count("/adGroups/report") == 1
    assert not [c for c in fake.calls if c["path"] == "/campaigns/report"]


def test_scan_all이면_꺼진_캠페인도_지출로_확인한다(monkeypatch, fake):
    _structure(monkeypatch, [{"id": 1, "name": "켜짐", "config": "ON"},
                             {"id": 2, "name": "꺼짐", "config": "OFF"}])
    T = kakao_api.today_kst()
    ads = kakao_api.fetch(9, T - dt.timedelta(1), T - dt.timedelta(1), scan_all=True)
    assert set(ads["캠페인"]) == {"켜짐", "꺼짐"}      # 둘 다 지출이 있는 가짜라서
    assert len([c for c in fake.calls if c["path"] == "/campaigns/report"]) == 2


def test_지출없는_캠페인의_소재는_부르지_않는다(monkeypatch):
    """소재 리포트가 호출의 대부분이다. 캠페인 합계로 걸러 내는 게 핵심."""
    _structure(monkeypatch, [{"id": 1, "name": "켜짐", "config": "ON"}])
    보낸 = []

    def zero_group(path, account, token, timeout=40, **params):
        보낸.append(path)
        비었음 = {"data": [{"start": "2026-10-19", "end": "2026-10-19",
                          "dimensions": {}, "metrics": {}}]}
        return 비었음

    monkeypatch.setattr(kakao_api, "_get", zero_group)
    T = kakao_api.today_kst()
    ads = kakao_api.fetch(9, T - dt.timedelta(1), T - dt.timedelta(1))
    assert ads.empty
    assert "/creatives/report" not in 보낸


# ---------------------------------------------------------------- 집계·전환
def test_normalize는_메타와_같은_열을_준다():
    df = kakao_api.normalize([{
        "날짜": "2026-10-19", "캠페인": "c", "광고세트": "g", "소재": "a",
        "광고ID": "1", "지출": 1000.0, "노출수": 10.0, "클릭수": 1.0}])
    assert list(df.columns) == kakao_api.KAKAO_COLUMNS
    assert df.loc[0, "날짜"] == dt.date(2026, 10, 19)


def test_지출이_없는_행은_버린다():
    df = kakao_api.normalize([
        {"날짜": "2026-10-19", "캠페인": "c", "광고세트": "g", "소재": "a",
         "광고ID": "1", "지출": 0.0, "노출수": 5.0, "클릭수": 0.0}])
    assert df.empty


def test_전환은_kakao_만_골라_키를_붙인다():
    db = pd.DataFrame({
        "날짜": [dt.date(2026, 10, 19)] * 3,
        "utm_source": ["kakao", "kakaopay", "meta"],
        "utm_campaign": ["x"] * 3,
        "utm_content": ["법무법인 평온 - kakao8_1029x258", "kakaopay_ad1-6", "노말"],
        "진행불가": [0, 0, 0], "접수": [1, 1, 1], "미팅": [0, 0, 0], "승인": [0, 0, 0],
        "구분": ["접수"] * 3})
    out = kakao_api.conversions(db)
    assert len(out) == 1
    assert out.loc[0, "key"] == "법무법인 평온 - kakao8_1029x258".lower()


def test_꼬리표_달린_소재에_전환이_붙는다():
    """실제 사례: 지출은 '..._메인' 소재에, 전환은 꼬리표 없는 이름으로 들어온다."""
    ads = pd.DataFrame({
        "날짜": [dt.date(2026, 10, 19)] * 2,
        "캠페인": ["1008 참여형"] * 2,
        "광고세트": ["테스트 소재 세트 / 납입금 절감"] * 2,
        "소재": ["법무법인 평온 - kakao13_1029x258 / 메인",
                "법무법인 평온 - kakao8_1029x258_메인"],
        "광고ID": ["1", "2"],
        "지출": [1274386.0, 673817.0], "노출수": [100.0, 50.0], "클릭수": [10.0, 5.0]})
    conv = pd.DataFrame({
        "날짜": [dt.date(2026, 10, 19)] * 3,
        "utm_source": ["kakao"] * 3,
        "utm_campaign": ["세트"] * 3,
        "utm_content": ["법무법인 평온 - kakao13_1029x258",
                        "법무법인 평온 - kakao13_1029x258",
                        "법무법인 평온 - kakao8_1029x258"],
        "진행불가": [0, 0, 0], "접수": [1, 0, 1], "미팅": [0, 0, 1], "승인": [0, 0, 0],
        "구분": ["접수", "미분류", "미팅"]})
    conv["key"] = conv["utm_content"].map(kakao_api.match_key)

    g = meta_table.build(ads, conv, key=kakao_api.match_key)
    k13 = g[g["소재"].str.contains("kakao13")].iloc[0]
    k8 = g[g["소재"].str.contains("kakao8")].iloc[0]
    assert (int(k13["전환수"]), int(k8["전환수"])) == (2, 1)
    assert meta_table.unmatched(g, conv, key=kakao_api.match_key).empty


def test_다른_계정_소재의_전환은_미매칭에서_빼준다():
    """메인·서브 전환이 시트에서 모두 utm_source=kakao 라, 그냥 두면 서로를 미매칭으로 센다."""
    ads = pd.DataFrame({
        "날짜": [dt.date(2026, 10, 19)],
        "캠페인": ["메인 캠페인"], "광고세트": ["메인 세트"],
        "소재": ["[비즈니스폼+] new kakao3_1029x258"], "광고ID": ["1"],
        "지출": [100.0], "노출수": [10.0], "클릭수": [1.0]})
    conv = pd.DataFrame({
        "날짜": [dt.date(2026, 10, 19)] * 2,
        "utm_source": ["kakao"] * 2, "utm_campaign": ["세트"] * 2,
        "utm_content": ["[비즈니스폼+] new kakao3_1029x258", "법무법인 평온 - kakao13_1029x258"],
        "진행불가": [0, 0], "접수": [1, 1], "미팅": [0, 0], "승인": [0, 0],
        "구분": ["접수"] * 2})
    conv["key"] = conv["utm_content"].map(kakao_api.match_key)
    g = meta_table.build(ads, conv, key=kakao_api.match_key)

    그냥 = meta_table.unmatched(g, conv, key=kakao_api.match_key)
    assert len(그냥) == 1                      # 서브 계정 소재가 미매칭으로 뜬다

    서브소재 = {kakao_api.match_key("법무법인 평온 - kakao13_1029x258")}
    빼고 = meta_table.unmatched(g, conv, key=kakao_api.match_key, extra_known=서브소재)
    assert 빼고.empty


# ---------------------------------------------------------------- 설정
def test_계정_ID는_설정으로_덮을_수_있다():
    기본 = kakao_api.accounts()
    assert set(기본) == {"카카오 메인", "카카오 서브"}
    덮음 = kakao_api.accounts({"카카오 메인": "123456"})
    assert 덮음["카카오 메인"] == 123456
    assert 덮음["카카오 서브"] == 기본["카카오 서브"]
    assert kakao_api.accounts({"카카오 메인": "숫자아님"}) == 기본


def test_토큰이_없으면_안내한다(monkeypatch, tmp_path):
    monkeypatch.delenv("KAKAO_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(kakao_api, "HERE", tmp_path / "dashboard")
    with pytest.raises(kakao_api.KakaoError) as e:
        kakao_api.credentials()
    assert "kakao_token" in str(e.value)


def test_재시도_대상에_호출제한과_일시장애가_들어있다():
    """429 만 재시도하다 503 'upstream connect error' 로 조회가 통째로 실패했다."""
    for code in (429, 500, 502, 503, 504):
        assert code in kakao_api.RETRY_STATUS
    assert 400 not in kakao_api.RETRY_STATUS
    assert 401 not in kakao_api.RETRY_STATUS


# ---------------------------------------------------------------- 세트 합계 검증
class FakeApi:
    """캠페인 지출과 소재별 지출을 따로 정해 두는 대역. 어떤 소재를 불렀는지 기록한다."""

    def __init__(self, 캠페인지출: float, 소재지출: dict[int, float]):
        self.캠페인지출 = 캠페인지출
        self.소재지출 = 소재지출
        self.불린소재: list[int] = []

    def install(self, monkeypatch, 소재목록):
        kakao_api.forget_spenders()      # 지난 테스트가 기억한 소재가 순서를 바꾸지 않게
        monkeypatch.setattr(kakao_api, "credentials", lambda token=None: "tok")
        monkeypatch.setattr(kakao_api, "campaigns",
                            lambda a, t: [{"id": 1, "name": "캠페인", "config": "ON"}])
        monkeypatch.setattr(kakao_api, "ad_groups",
                            lambda a, t, cid: [{"id": 7, "name": "세트"}])
        monkeypatch.setattr(kakao_api, "creatives", lambda a, t, gid: 소재목록)
        monkeypatch.setattr(kakao_api, "_get", self._get)

    def _get(self, path, account, token, timeout=40, **params):
        day = (kakao_api.today_kst() - dt.timedelta(1)).isoformat()
        if path in ("/campaigns/report", "/adGroups/report"):
            # 가짜 계정은 세트가 하나라 세트 지출 = 캠페인 지출이다
            비용 = self.캠페인지출
        else:
            cid = int(params["creativeId"])
            self.불린소재.append(cid)
            비용 = self.소재지출.get(cid, 0.0)
        return {"data": [{"start": day, "end": day,
                          "dimensions": {"creative_id": str(params.get("creativeId", ""))},
                          "metrics": {"imp": 10, "click": 1, "cost": 비용}}]}


소재목록 = [{"id": 1, "name": "켜진소재", "config": "ON"},
           {"id": 2, "name": "꺼진소재A", "config": "OFF"},
           {"id": 3, "name": "꺼진소재B", "config": "OFF"}]


def _어제():
    T = kakao_api.today_kst()
    return T - dt.timedelta(1), T - dt.timedelta(1)


def test_캠페인_합계가_설명되면_꺼진_소재는_부르지_않는다(monkeypatch):
    """꺼진 소재까지 전부 부르면 세트 2개에 9분이 걸렸다."""
    api = FakeApi(캠페인지출=1000.0, 소재지출={1: 1000.0, 2: 500.0, 3: 500.0})
    api.install(monkeypatch, 소재목록)
    ads = kakao_api.fetch(9, *_어제())
    assert api.불린소재 == [1]                      # 켜진 소재 하나로 끝
    assert ads["지출"].sum() == 1000.0
    assert ads.attrs["누락"] == []


def test_켜진_소재로_모자라면_꺼진_소재까지_채운다(monkeypatch):
    """서브 계정 10/19: 어제 돌다 끈 소재 때문에 지출의 57%(312,420원)가 사라졌었다."""
    api = FakeApi(캠페인지출=543859.0, 소재지출={1: 231439.0, 2: 312420.0, 3: 0.0})
    api.install(monkeypatch, 소재목록)
    ads = kakao_api.fetch(9, *_어제())
    assert ads["지출"].sum() == 543859.0            # 캠페인 합계와 정확히 일치
    assert api.불린소재 == [1, 2]                    # 메꿔지면 3번은 안 부른다
    assert ads.attrs["누락"] == []


def test_끝까지_안_맞으면_누락으로_남긴다(monkeypatch):
    """삭제된 소재의 지출 등. 조용히 비면 안 된다."""
    api = FakeApi(캠페인지출=1000.0, 소재지출={1: 100.0, 2: 0.0, 3: 0.0})
    api.install(monkeypatch, 소재목록)
    ads = kakao_api.fetch(9, *_어제())
    assert sorted(api.불린소재) == [1, 2, 3]          # 전부 불러 봤다
    (누락,) = ads.attrs["누락"]
    assert 누락["세트지출"] == 1000.0
    assert 누락["소재합계"] == 100.0
    assert 누락["누락"] == 900.0


def test_켜진_소재를_먼저_부른다():
    섞임 = [{"id": 9, "config": "OFF"}, {"id": 1, "config": "ON"},
           {"id": 8, "config": "OFF"}, {"id": 2, "config": "ON"}]
    assert [c["id"] for c in kakao_api.order_creatives(섞임)] == [1, 2, 9, 8]


def test_지출없는_캠페인은_소재를_아예_부르지_않는다(monkeypatch):
    api = FakeApi(캠페인지출=0.0, 소재지출={1: 100.0})
    api.install(monkeypatch, 소재목록)
    ads = kakao_api.fetch(9, *_어제())
    assert api.불린소재 == [] and ads.empty


# ---------------------------------------------------------------- 토큰 출처
def test_토큰은_대시보드_설정에서_읽는다(monkeypatch, tmp_path):
    """kakao_bizform 폴더는 은퇴했다. 그 폴더가 없어도 돌아가야 한다."""
    monkeypatch.delenv("KAKAO_ACCESS_TOKEN", raising=False)
    (tmp_path / "config.json").write_text('{"kakao_token": "내토큰"}', encoding="utf-8")
    monkeypatch.setattr(kakao_api, "HERE", tmp_path)
    assert kakao_api.credentials() == "내토큰"


def test_넘긴_토큰이_설정보다_우선(monkeypatch, tmp_path):
    """클라우드에서는 app 이 Secrets 값을 인자로 넘긴다."""
    (tmp_path / "config.json").write_text('{"kakao_token": "파일것"}', encoding="utf-8")
    monkeypatch.setattr(kakao_api, "HERE", tmp_path)
    assert kakao_api.credentials("넘긴것") == "넘긴것"


def test_레거시_폴더가_없어도_안내만_하고_끝난다(monkeypatch, tmp_path):
    monkeypatch.delenv("KAKAO_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(kakao_api, "HERE", tmp_path / "없는폴더")
    with pytest.raises(kakao_api.KakaoError) as e:
        kakao_api.credentials()
    assert "kakao_token" in str(e.value)


def test_401은_만료가_아니라고_안내한다(monkeypatch):
    """이 토큰은 만료되지 않는다. '만료' 라고 안내하면 엉뚱한 곳을 보게 된다."""
    class R:
        status_code = 401
        text = "unauthorized"

    class FakeRequests:
        class RequestException(Exception):
            pass

        @staticmethod
        def get(*a, **k):
            return R()

    monkeypatch.setitem(sys.modules, "requests", FakeRequests)
    with pytest.raises(kakao_api.KakaoError) as e:
        kakao_api._get("/campaigns", 1, "tok")
    말 = str(e.value)
    assert "만료되지 않" in 말
    assert "issue_token.py" in 말


def test_클라우드_Secrets_가_올바른_TOML_이다(tmp_path, monkeypatch):
    """`카카오 메인` 처럼 공백이 있는 키는 TOML 에서 따옴표가 필요하다.

    안 감싸면 Secrets 전체가 파싱에 실패해 앱이 시트 주소조차 못 읽는다.
    """
    import re
    import tomllib

    import make_cloud_secrets as mcs

    cfg = {"ad_sheet_id": "SHEET", "kakao_token": "토큰값",
           "kakao_accounts": {"카카오 메인": 802036, "카카오 서브": 928346}}
    (tmp_path / "config.json").write_text(__import__("json").dumps(cfg), encoding="utf-8")
    (tmp_path / "service_account.json").write_text(
        '{"type": "service_account", "project_id": "p", "private_key": "k",'
        ' "client_email": "e@x.iam.gserviceaccount.com", "token_uri": "t"}', encoding="utf-8")
    monkeypatch.setattr(mcs, "HERE", tmp_path)
    monkeypatch.setattr(mcs, "META", {})
    monkeypatch.setattr(sys, "argv", ["make_cloud_secrets.py"])

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mcs.main()
    본문 = re.search(r"-{4} 아래.*?-{4}\n(.*?)\n-{4} 여기까지", buf.getvalue(), re.S).group(1)

    d = tomllib.loads(본문)                      # 깨지면 여기서 예외
    assert d["kakao_token"] == "토큰값"
    assert d["kakao_accounts"]["카카오 메인"] == 802036
    assert d["kakao_accounts"]["카카오 서브"] == 928346


# ---------------------------------------------------------------- 계정별 전환 분리
def _conv(contents):
    d = pd.DataFrame({
        "날짜": [dt.date(2026, 10, 19)] * len(contents),
        "utm_source": ["kakao"] * len(contents),
        "utm_campaign": ["세트"] * len(contents),
        "utm_content": contents,
        "진행불가": [0] * len(contents), "접수": [1] * len(contents),
        "미팅": [0] * len(contents), "승인": [0] * len(contents),
        "구분": ["접수"] * len(contents)})
    d["key"] = d["utm_content"].map(kakao_api.match_key)
    return d


메인소재 = "[비즈니스폼+] new kakao3_1029x258"
서브소재 = "법무법인 평온 - kakao13_1029x258"
유령소재 = "삭제된_소재_xyz"
MAIN, SUB = 802036, 928346
IDX = {SUB: {kakao_api.match_key(서브소재)}}


def test_서브는_자기_소재명_목록에_있는_전환만():
    conv = _conv([메인소재, 서브소재, 서브소재, 유령소재])
    out = kakao_api.split_conversions(conv, SUB, primary=MAIN, indexes=IDX)
    assert list(out["utm_content"]) == [서브소재, 서브소재]


def test_메인은_다른_계정_목록에_없는_나머지_전부():
    """메인은 캠페인이 21개라 소재명 목록을 만들지 않는다. 여집합으로 가져간다."""
    conv = _conv([메인소재, 서브소재, 서브소재, 유령소재])
    out = kakao_api.split_conversions(conv, MAIN, primary=MAIN, indexes=IDX)
    assert sorted(out["utm_content"]) == sorted([메인소재, 유령소재])


def test_두_계정이_전환을_빠짐없이_한_번씩_나눠_갖는다():
    """어느 쪽에도 안 들어가거나 양쪽에 중복되면 집계가 틀어진다.

    split_conversions 는 인덱스를 다시 매기므로, 행을 알아볼 표시를 붙여 비교한다.
    """
    conv = _conv([메인소재, 서브소재, 유령소재, 메인소재, 서브소재])
    conv["행번호"] = range(len(conv))
    메인것 = kakao_api.split_conversions(conv, MAIN, primary=MAIN, indexes=IDX)
    서브것 = kakao_api.split_conversions(conv, SUB, primary=MAIN, indexes=IDX)

    메인행, 서브행 = set(메인것["행번호"]), set(서브것["행번호"])
    assert not (메인행 & 서브행), "같은 전환이 두 계정에 중복 집계된다"
    assert 메인행 | 서브행 == set(conv["행번호"]), "어느 계정에도 안 들어간 전환이 있다"
    assert 서브행 == {1, 4}


def test_꼬리표가_달라도_계정이_갈린다():
    """지출 쪽 소재명은 `법무법인 평온 - kakao13_1029x258 / 메인` 처럼 꼬리표가 붙어 있다."""
    idx = {SUB: {kakao_api.match_key("법무법인 평온 - kakao13_1029x258 / 메인")}}
    conv = _conv([서브소재])
    out = kakao_api.split_conversions(conv, SUB, primary=MAIN, indexes=idx)
    assert len(out) == 1


def test_빈_전환표는_그대로():
    assert kakao_api.split_conversions(None, SUB, MAIN, IDX) is None
    빈 = _conv([]).iloc[0:0]
    assert kakao_api.split_conversions(빈, SUB, MAIN, IDX).empty


def test_소재명_목록은_꺼진_캠페인까지_모은다(monkeypatch):
    """지출 조회는 켜진 캠페인만 보지만, 전환을 가르는 목록은 전부여야 한다.

    서브 계정은 꺼진 캠페인(소재 13개) + 켜진 캠페인(27개) = 39종이다.
    꺼진 쪽을 빼면 그 소재의 전환이 메인 쪽으로 잘못 넘어간다.
    """
    monkeypatch.setattr(kakao_api, "credentials", lambda token=None: "tok")
    monkeypatch.setattr(kakao_api, "campaigns", lambda a, t: [
        {"id": 1, "name": "꺼진캠페인", "config": "OFF"},
        {"id": 2, "name": "켜진캠페인", "config": "ON"}])
    monkeypatch.setattr(kakao_api, "ad_groups",
                        lambda a, t, cid: [{"id": cid * 10, "config": "OFF"}])
    monkeypatch.setattr(kakao_api, "creatives", lambda a, t, gid: [
        {"id": gid, "name": f"소재{gid}_메인", "config": "OFF"}])
    idx = kakao_api.creative_index(9)
    assert idx == {"소재10", "소재20"}        # 꼬리표를 뗀 키로, 꺼진 캠페인 것도 들어 있다


# ---------------------------------------------------------------- 지출 소재 기억
def test_지난번_지출_소재를_먼저_부른다(monkeypatch):
    """지출은 소재 몇 개에 몰려 있고 구성이 잘 안 바뀐다. 기억해 두면 호출이 줄어든다."""
    # 꺼진 소재 3번만 지출이 있는 상황 (켜진 1번은 지출 0)
    api = FakeApi(캠페인지출=500.0, 소재지출={1: 0.0, 2: 0.0, 3: 500.0})
    api.install(monkeypatch, 소재목록)
    kakao_api.fetch(9, *_어제())
    첫판 = list(api.불린소재)
    assert 첫판 == [1, 2, 3]                  # 켜진 것부터 더듬어 3번에 도달

    api.불린소재.clear()
    kakao_api.fetch(9, *_어제())              # 기억이 살아 있는 두 번째 조회
    assert api.불린소재 == [3], "기억한 소재를 먼저 불러 한 번에 끝나야 한다"


def test_기억은_지출이_있던_소재만(monkeypatch):
    api = FakeApi(캠페인지출=500.0, 소재지출={1: 500.0, 2: 0.0, 3: 0.0})
    api.install(monkeypatch, 소재목록)
    kakao_api.fetch(9, *_어제())
    assert kakao_api.remembered(9, 1) == [1]


def test_기억이_틀려도_캠페인_합계로_메꾼다(monkeypatch):
    """기억한 소재가 이제 안 돌면, 합계가 안 맞으니 나머지를 계속 부른다."""
    api = FakeApi(캠페인지출=500.0, 소재지출={1: 0.0, 2: 0.0, 3: 500.0})
    api.install(monkeypatch, 소재목록)
    kakao_api.fetch(9, *_어제())              # 3번을 기억
    kakao_api.forget_spenders()
    kakao_api.remember(9, 1, 2)               # 엉뚱한 소재(2번)를 기억한 상태로 바꾼다

    api.불린소재.clear()
    ads = kakao_api.fetch(9, *_어제())
    assert api.불린소재[0] == 2                # 기억한 것을 먼저 부르고
    assert 3 in api.불린소재                   # 모자라니 계속 불러 3번까지 간다
    assert ads["지출"].sum() == 500.0
    assert ads.attrs["누락"] == []


def test_기억_목록은_무한히_늘지_않는다():
    kakao_api.forget_spenders()
    for i in range(kakao_api.SPENDERS_MAX + 15):
        kakao_api.remember(1, 1, i)
    assert len(kakao_api.remembered(1, 1)) == kakao_api.SPENDERS_MAX


def test_우선순위_기억_켜짐_꺼짐_순():
    쌍 = [({}, {"id": 9, "config": "OFF"}), ({}, {"id": 1, "config": "ON"}),
         ({}, {"id": 8, "config": "OFF"}), ({}, {"id": 2, "config": "ON"})]
    순서 = [cr["id"] for _, cr in kakao_api.prioritize(쌍, 기억=[8])]
    assert 순서 == [8, 1, 2, 9]
