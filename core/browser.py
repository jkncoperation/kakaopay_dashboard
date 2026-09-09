"""브라우저 확보 방식 두 가지 - 전용 프로필 / 이미 켜진 Chrome 에 붙기.

전용 프로필(profile)
    이 폴더 안의 프로필로 Chrome 을 띄운다. 카카오·구글에 각각 1회 로그인이 필요하지만,
    그 프로필에는 이 업무 계정만 들어 있어 안전하다. 기본값.

붙기(cdp)
    `--remote-debugging-port` 로 켜 둔 본인 Chrome 에 연결한다. 로그인이 아예 필요 없다.
    대신 그 포트에 붙을 수 있는 로컬 프로그램은 그 Chrome 의 모든 로그인 세션을 쓸 수 있으므로,
    수집할 때만 켜거나 업무 전용 프로필로 띄우는 것을 권한다.

Chromium 대신 실제 Chrome(channel="chrome")을 쓴다. 구글이 자동화용 Chromium 의 로그인을
막는 경우가 있어서다. Chrome 이 없으면 Chromium 으로 자동 폴백한다.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

DEFAULT_CDP = "http://127.0.0.1:9222"


@contextlib.contextmanager
def browser_context(mode: str = "profile", profile_dir: str | Path = "chrome-profile",
                    headless: bool = True, cdp_url: str = DEFAULT_CDP,
                    viewport: dict | None = None):
    """with 문으로 쓰는 브라우저 컨텍스트. mode: 'profile' | 'cdp'."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if mode == "cdp":
            try:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=10_000)
            except Exception as exc:
                raise RuntimeError(
                    f"켜져 있는 Chrome 에 붙지 못했습니다 ({cdp_url}).\n"
                    "Chrome 을 원격 디버깅 옵션으로 켠 뒤 다시 시도해 주세요:\n"
                    '  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                    "--remote-debugging-port=9222\n"
                    f"(원인: {exc})") from exc
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            try:
                yield ctx
            finally:
                # 붙은 브라우저는 사용자 것이므로 절대 닫지 않는다. 연결만 끊는다.
                with contextlib.suppress(Exception):
                    browser.close()
            return

        profile = Path(profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        kw = dict(headless=headless, locale="ko-KR", accept_downloads=True,
                  viewport=viewport or {"width": 1500, "height": 950})
        try:
            ctx = p.chromium.launch_persistent_context(str(profile), channel="chrome", **kw)
        except Exception:
            ctx = p.chromium.launch_persistent_context(str(profile), **kw)   # Chrome 없으면 Chromium
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def first_page(ctx):
    """컨텍스트의 첫 페이지(없으면 새로 연다)."""
    return ctx.pages[0] if ctx.pages else ctx.new_page()


# ---------------------------------------------------------------- 세션 보존
# 카카오 로그인 쿠키는 '세션 쿠키'라 브라우저를 닫으면 프로필에서 사라진다.
# ('로그인 상태 유지' 를 체크하지 않은 경우) 그래서 로그인 직후 메모리에 있는
# 쿠키·localStorage 를 파일로 떠 두었다가, 다음 실행 때 그대로 주입한다.

def save_state(ctx, path) -> int:
    """현재 컨텍스트의 쿠키·localStorage 를 파일로 저장. 저장한 쿠키 수를 돌려준다."""
    import json
    from pathlib import Path as _P
    p = _P(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state = ctx.storage_state()
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return len(state.get("cookies") or [])


def restore_state(ctx, path, origins_like=("kakao", "kakaopay")) -> tuple[int, int]:
    """저장해 둔 쿠키·localStorage 를 컨텍스트에 주입. (쿠키 수, localStorage 항목 수)."""
    import json
    from pathlib import Path as _P
    p = _P(path)
    if not p.exists():
        return 0, 0
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0

    cookies = state.get("cookies") or []
    if cookies:
        with contextlib.suppress(Exception):
            ctx.add_cookies(cookies)

    n_ls = 0
    origins = [o for o in (state.get("origins") or [])
               if any(k in o.get("origin", "") for k in origins_like)]
    if origins:
        page = ctx.new_page()
        for o in origins:
            items = o.get("localStorage") or []
            if not items:
                continue
            try:
                page.goto(o["origin"], wait_until="domcontentloaded", timeout=30_000)
                page.evaluate(
                    "items => { for (const it of items) localStorage.setItem(it.name, it.value); }",
                    items)
                n_ls += len(items)
            except Exception:
                continue
        with contextlib.suppress(Exception):
            page.close()
    return len(cookies), n_ls
