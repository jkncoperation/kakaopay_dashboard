"""광고센터 화면에서 당일 실시간 성과를 읽어 온다 - 로그인된 크롬을 그대로 쓴다.

카카오페이 광고센터는 공개 API 가 없다. 그래서 로그인 상태를 유지한 크롬(Playwright)으로
화면의 표를 직접 읽는다. 사람이 다운로드 파일을 올리지 않아도 당일 탭이 채워지게 하는 것이 목적이다.

읽는 순서는 사람이 화면에서 하는 것과 같다.

    1. 캠페인은 미선택 상태로 광고그룹 탭을 연다 (campaignId 파라미터를 붙이지 않는다)
    2. 오늘 지출이 있는 광고그룹만 고른다
    3. 그 그룹만 선택한 채 소재 탭으로 넘어간다
       (화면에서 좌측 체크박스를 켜고 '소재' 를 누르면 `/content?...&adGroupId=1,2` 로 가므로 같은 URL 로 간다.
        그 경로가 막히면 실제로 체크하고 탭을 누르는 방식으로 넘어간다.)
    4. 소재 표를 읽어 표준 스키마(AD_COLUMNS)로 돌려준다

소재 탭을 필터 없이 열면 100행에서 잘려 정작 지출 중인 소재가 빠진다. 그래서 그룹을 먼저 고른다.

표 열은 **헤더 이름으로** 찾는다. 광고센터가 열 순서를 바꿔도 따라가고, 이름을 못 찾으면
화면에서 확인한 기본 순서로 돌아간다(그 사실을 화면에 알린다).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import pandas as pd

from core.util import today_kst
from parsers.adcenter_file import AD_COLUMNS

HERE = Path(__file__).resolve().parents[1]
BASE = "https://adcenter.kakaopay.com"


class CollectError(Exception):
    pass


# ---------------------------------------------------------------- 설정
DEFAULTS = {
    "ad_account_id": "10000246",
    "profile_dir": "chrome-profile",     # 카카오 로그인 상태가 저장되는 곳 (git 제외)
    "headless": True,
    "interval_min": 10,
    "align_to_clock": True,              # 시계에 맞춰 :00 :10 :20 … 에 갱신 (False 면 시작 시각 기준)
    "min_spend": 1,                      # 오늘 이 금액 이상 쓴 광고그룹만 (0 이면 전체)
    "size": 100,                         # 한 화면에 불러올 행 수
    "relogin_wait_min": 120,             # 자동 로그인이 막혔을 때 창을 띄우고 기다리는 시간
}


def collector_config(cfg: dict | None = None) -> dict:
    """config.json 의 값 + 기본값. (collector 아래에 넣어도 되고 최상위에 둬도 된다)"""
    out = dict(DEFAULTS)
    cfg = cfg or {}
    for key in DEFAULTS:
        if key in cfg:
            out[key] = cfg[key]
    out.update({k: v for k, v in (cfg.get("collector") or {}).items() if k in DEFAULTS})
    return out


# ---------------------------------------------------------------- URL
def group_list_url(c: dict, day: dt.date) -> str:
    """캠페인 미선택 상태의 광고그룹 목록 - 캠페인을 걸면 다른 캠페인 그룹이 빠진다."""
    return (f"{BASE}/ad-management/{c['ad_account_id']}/group"
            f"?startDate={day}&endDate={day}&size={c['size']}")


def content_url(c: dict, day: dt.date, group_ids: list[str]) -> str:
    url = (f"{BASE}/ad-management/{c['ad_account_id']}/content"
           f"?startDate={day}&endDate={day}&size={c['size']}")
    if group_ids:
        url += "&adGroupId=" + ",".join(group_ids)     # 화면에서 그룹을 체크했을 때와 같은 형태
    return url


# ---------------------------------------------------------------- 표 읽기
JS_READ_TABLE = """
() => {
  const table = [...document.querySelectorAll('table')].find(t => t.querySelector('tbody tr'));
  if (!table) return { head: [], rows: [] };
  const head = [...table.querySelectorAll('thead th')].map(th => th.innerText.replace(/\\s+/g, ' ').trim());
  const onIdx = head.findIndex(h => /on\\s*\\/?\\s*off/i.test(h));
  const rows = [...table.querySelectorAll('tbody tr')].map(tr => {
    const tds = [...tr.querySelectorAll('td')];
    const t = tds.map(td => td.innerText.replace(/\\s+/g, ' ').trim());
    const cell = tds[onIdx >= 0 ? onIdx : 3];
    let on = null;
    if (cell) {
      const sw = cell.querySelector('input[type=checkbox], [role=switch], [aria-checked]');
      if (sw) on = (sw.checked === true) || sw.getAttribute('aria-checked') === 'true';
    }
    return { t, on };
  });
  return { head, rows };
}
"""

# 광고그룹 표: 0체크 1ID 2이름 3ON/OFF 4상태 5캠페인 6상품 7입찰가 8그룹예산 9일예산 10소진 …
GROUP_COLS = {"id": ["광고그룹id", "그룹id", "id"],
              "name": ["광고그룹", "광고그룹이름", "광고그룹명", "그룹명"],
              "status": ["상태"],
              "spend": ["소진비용", "소진"]}
GROUP_FALLBACK = {"id": 1, "name": 2, "status": 4, "spend": 10}

# 소재 표: 0빈칸 1소재ID 2소재 3ON/OFF 4상태 5상위광고그룹 6광고상품 7소진 8노출 9클릭 10클릭률 11도달 12eCPM 13CPC 14기간
CONTENT_COLS = {"소재": ["소재"],
                "상태": ["상태"],
                "광고그룹": ["상위광고그룹이름", "상위광고그룹", "광고그룹"],
                "광고상품": ["광고상품"],
                "소진비용": ["소진비용", "소진"],
                "노출수": ["노출수", "노출"],
                "클릭수": ["클릭수", "클릭"],
                "클릭률": ["클릭률"],
                "도달수": ["도달수", "도달"],
                "eCPM": ["ecpm"],
                "CPC": ["cpc"]}
CONTENT_FALLBACK = {"소재": 2, "상태": 4, "광고그룹": 5, "광고상품": 6, "소진비용": 7,
                    "노출수": 8, "클릭수": 9, "클릭률": 10, "도달수": 11, "eCPM": 12, "CPC": 13}


def norm(s) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(s or "").lower())


def map_columns(head: list[str], ncells: int, names: dict[str, list[str]],
                fallback: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    """헤더 이름으로 열 위치를 찾는다. (인덱스 표, 이름으로 못 찾아 기본값을 쓴 항목들)"""
    head = list(head)
    if head and len(head) + 1 == ncells:        # 맨 앞 체크박스 열에 th 가 없는 경우
        head = [""] + head
    normed = [norm(h) for h in head]
    idx, fell_back = {}, []
    for key, cands in names.items():
        found = next((normed.index(norm(c)) for c in cands if norm(c) in normed), -1)
        if found < 0:
            found = fallback[key]
            fell_back.append(key)
        idx[key] = found
    return idx, fell_back


def _wait_rows(page, timeout: int = 20_000) -> bool:
    """표 뼈대가 먼저 그려지고 데이터는 나중에 채워진다. 칸이 실제로 들어찰 때까지 기다린다."""
    try:
        page.wait_for_selector("table tbody tr", timeout=timeout)
        page.wait_for_function(
            "() => [...document.querySelectorAll('table tbody tr')].some(tr => tr.querySelectorAll('td').length > 3)",
            timeout=timeout)
    except Exception:
        return False
    time.sleep(1)
    return True


def read_groups(page, c: dict, day: dt.date, verbose: bool = False) -> list[dict]:
    """오늘 광고그룹 목록 (id, 이름, 상태, 소진)."""
    page.goto(group_list_url(c, day), wait_until="networkidle", timeout=60_000)
    check_logged_in(page)
    if not _wait_rows(page):
        return []
    data = page.evaluate(JS_READ_TABLE)
    rows = data["rows"]
    if not rows:
        return []
    idx, fell_back = map_columns(data["head"], len(rows[0]["t"]), GROUP_COLS, GROUP_FALLBACK)
    if verbose and fell_back:
        print(f"  [광고그룹 표] 이름으로 못 찾아 기본 순서를 쓴 열: {', '.join(fell_back)}")
    need = max(idx.values()) + 1
    out = []
    for r in rows:
        t = r["t"]
        if len(t) < need:
            continue
        out.append({"id": t[idx["id"]], "name": t[idx["name"]], "status": t[idx["status"]],
                    "spend": _num(t[idx["spend"]])})
    return out


def _num(s) -> float:
    s = str(s or "").replace(",", "").replace("원", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _clean(v: str) -> str:
    """'53,400원' → '53,400' (클릭률의 % 는 남긴다 - 읽는 쪽이 비율로 바꿔 준다)"""
    return str(v or "").replace("원", "").strip()


def open_content(page, c: dict, day: dt.date, group_ids: list[str]) -> bool:
    """URL 로 바로 소재 탭을 연다. 비어 있으면 화면에서 직접 체크 → '소재' 탭 클릭으로 재시도."""
    page.goto(content_url(c, day, group_ids), wait_until="networkidle", timeout=60_000)
    check_logged_in(page)
    if _wait_rows(page):
        return True
    print("  ! adGroupId URL 로 소재가 안 나와 화면 조작으로 재시도합니다")
    page.goto(group_list_url(c, day), wait_until="networkidle", timeout=60_000)
    if not _wait_rows(page):
        return False
    page.evaluate("""(ids) => {
      document.querySelectorAll('table tbody tr').forEach(tr => {
        const tds = tr.querySelectorAll('td');
        if (tds.length < 4) return;
        if (!ids.includes(tds[1].innerText.trim())) return;
        const lab = tds[0].querySelector('label');
        const inp = tds[0].querySelector('input[type=checkbox]');
        if (inp && !inp.checked) (lab || inp).click();
      });
    }""", group_ids)
    time.sleep(1)
    tab = page.get_by_role("link", name="소재")
    if not tab.count():
        tab = page.get_by_text("소재", exact=True)
    if not tab.count():
        return False
    tab.first.click()
    page.wait_for_load_state("networkidle", timeout=60_000)
    return _wait_rows(page)


def rows_to_frame(data: dict, day: dt.date, verbose: bool = False) -> pd.DataFrame:
    """소재 표 원자료 → 표준 스키마(AD_COLUMNS). 브라우저 없이도 테스트할 수 있게 분리."""
    rows = data.get("rows") or []
    if not rows:
        return pd.DataFrame(columns=AD_COLUMNS)
    idx, fell_back = map_columns(data.get("head", []), len(rows[0]["t"]), CONTENT_COLS, CONTENT_FALLBACK)
    if verbose:
        print(f"  [소재 표] 헤더 {len(data.get('head', []))}개 / 칸 {len(rows[0]['t'])}개"
              + (f" · 기본 순서를 쓴 열: {', '.join(fell_back)}" if fell_back else " · 전부 헤더 이름으로 찾음"))
    need = max(idx.values()) + 1
    out = []
    for r in rows:
        t = r["t"]
        if len(t) < need:                 # 합계 행처럼 칸이 모자란 줄
            continue
        row = {"날짜": day, "ON/OFF": "" if r["on"] is None else ("ON" if r["on"] else "OFF")}
        for col in ("소재", "상태", "광고그룹", "광고상품"):
            row[col] = t[idx[col]]
        for col in ("소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC"):
            row[col] = _clean(t[idx[col]])
        out.append(row)
    return pd.DataFrame(out, columns=AD_COLUMNS)


def collect_today(page, c: dict, day: dt.date | None = None,
                  verbose: bool = False) -> tuple[pd.DataFrame, list[dict]]:
    """(소재별 당일 성과, 고른 광고그룹들). 지출 있는 그룹이 없으면 빈 표."""
    day = day or today_kst()
    groups = read_groups(page, c, day, verbose=verbose)
    picked = [g for g in groups if g["spend"] >= c["min_spend"]]
    if verbose:
        print(f"  광고그룹 {len(groups)}개 중 오늘 {c['min_spend']}원 이상 쓴 그룹 {len(picked)}개: "
              + (", ".join(f"{g['name'][:22]}({g['spend']:,.0f})" for g in picked) or "없음"))
    if not picked:
        return pd.DataFrame(columns=AD_COLUMNS), []
    if not open_content(page, c, day, [g["id"] for g in picked]):
        raise CollectError("소재 표를 열지 못했습니다 (화면 구조가 바뀌었을 수 있습니다)")
    return rows_to_frame(page.evaluate(JS_READ_TABLE), day, verbose=verbose), picked


# ---------------------------------------------------------------- 로그인 · 세션
# 로그인 폼. 카카오는 같은 폼을 accounts.kakao.com 에도, partner.kakaopay.com/login/bridge 에도
# 띄운다. 그래서 URL 로 로그인 여부를 판단하면 안 되고, 폼이 떠 있는지로 봐야 한다.
ID_SELECTORS = ["input[name=loginId]", "input#loginId--1", "input[type=email]"]
PW_SELECTORS = ["input[name=password]", "input[type=password]"]


def login_form(page):
    """로그인 폼이 떠 있으면 (아이디칸, 비밀번호칸), 아니면 (None, None)."""
    id_box = next((page.query_selector(s) for s in ID_SELECTORS if page.query_selector(s)), None)
    pw_box = next((page.query_selector(s) for s in PW_SELECTORS if page.query_selector(s)), None)
    return (id_box, pw_box) if (id_box and pw_box) else (None, None)


def check_logged_in(page) -> None:
    if "accounts.kakao" in page.url or "/login" in page.url or login_form(page)[0]:
        raise CollectError("LOGIN_REQUIRED")


def load_env_credentials(env_path: Path | None = None) -> tuple[str | None, str | None]:
    """.env 의 KAKAO_ID / KAKAO_PW (없으면 None, None → 창을 띄워 직접 로그인)"""
    env_path = env_path or (HERE / ".env")
    if not env_path.exists():
        return None, None
    vals = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals.get("KAKAO_ID") or None, vals.get("KAKAO_PW") or None


# 로그인 상태 유지 체크박스. 이게 꺼져 있으면 세션이 금방 풀린다.
STAY_SIGNED_IN = ("input[name=staySignedIn], input[id^=staySignedIn], "
                  "input[name=saveSignedIn], input[id^=saveSignedIn]")


def check_stay_signed_in(page) -> bool:
    box = page.query_selector(STAY_SIGNED_IN)
    if not box:
        return False
    try:
        if box.is_checked():
            return True
    except Exception:
        pass
    for attempt in (lambda: box.check(), lambda: box.check(force=True),
                    lambda: page.eval_on_selector(STAY_SIGNED_IN, "e => { if (!e.checked) e.click(); }")):
        try:
            attempt()
            if box.is_checked():
                return True
        except Exception:
            continue
    return False


EXTRA_AUTH = ["2단계 인증", "추가 인증", "기기 확인", "보안문자", "인증번호", "새로운 기기"]
BAD_CREDS = ["비밀번호가 일치하지", "존재하지 않는", "계정 정보를 확인"]


def auto_login(page, c: dict, kid: str, kpw: str, timeout_s: int = 90) -> bool:
    """카카오 계정 폼에 아이디/비밀번호를 넣는다. 2단계 인증 화면이 뜨면 False(수동으로 넘김).

    로그인 폼은 accounts.kakao.com 에도 partner.kakaopay.com/login/bridge 에도 뜬다.
    그래서 URL 이 아니라 폼이 있는지로 판단한다.
    """
    page.goto(group_list_url(c, today_kst()), wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)
    id_box, pw_box = login_form(page)
    if not id_box:
        return _wait_rows(page)                      # 폼이 없으면 이미 로그인된 것
    id_box.click(); page.keyboard.press("Control+A"); page.keyboard.type(kid)
    pw_box.click(); page.keyboard.press("Control+A"); page.keyboard.type(kpw)
    check_stay_signed_in(page)
    submit = page.query_selector("button[type=submit]")
    if submit:
        submit.click()
    else:
        page.keyboard.press("Enter")                 # 폼 구조가 바뀌어도 엔터로는 넘어간다
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        body = (page.inner_text("body") or "")[:3000]
        if any(k in body for k in EXTRA_AUTH):
            return False
        if any(k in body for k in BAD_CREDS):
            print("  ! .env 의 아이디/비밀번호를 확인하세요")
            return False
        if not login_form(page)[0] and "adcenter.kakaopay.com" in page.url:
            return True                              # 폼이 사라지고 광고센터로 넘어왔다
    return False


def session_path(c: dict) -> Path:
    return HERE / c["profile_dir"] / "session_cookies.json"


def save_session(ctx, c: dict) -> None:
    """광고센터 쪽 쿠키(partner.kakaopay.com 의 __T_)는 브라우저를 닫으면 사라지는 세션 쿠키다.
    다시 켤 때 그대로 넣어 주려고 저장해 둔다."""
    try:
        p = session_path(c)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ctx.cookies(), ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"  ! 세션 저장 실패(무시하고 계속): {exc}")


def restore_session(ctx, c: dict) -> bool:
    p = session_path(c)
    if not p.exists():
        return False
    try:
        ctx.add_cookies(json.loads(p.read_text(encoding="utf-8")))
        return True
    except Exception as exc:
        print(f"  ! 세션 복원 실패(무시하고 계속): {exc}")
        return False


def open_context(pw, c: dict, headed: bool | None = None):
    """로그인 상태가 저장된 프로필로 크롬을 연다."""
    profile = (HERE / c["profile_dir"]).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    headless = (not headed) if headed is not None else c["headless"]
    ctx = pw.chromium.launch_persistent_context(str(profile), headless=headless,
                                                viewport={"width": 1400, "height": 900}, locale="ko-KR")
    restore_session(ctx, c)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page
