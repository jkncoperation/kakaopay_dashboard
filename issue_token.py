# -*- coding: utf-8 -*-
"""카카오모먼트 토큰 확인 · 재발급.

평소에는 쓸 일이 없다. **이 토큰은 만료되지 않는다.**
카카오 비즈니스 인증(`kauth.kakao.com/oauth/business/token`) 은 `expires_in` 도
`refresh_token` 도 없는 토큰을 내준다. 2026-07-24 부터 쓰던 토큰이 3개월 뒤에도
그대로 동작했고, 그걸 쓰던 동기화 스크립트에는 갱신 코드가 아예 없었다.

그래서 이 파일은 두 가지 때만 필요하다.
  · 지금 토큰이 어느 광고계정까지 보이는지 확인할 때        -> `--check`
  · 광고계정을 새로 추가해 권한이 바뀌었을 때 (재발급)       -> `url` / `exchange`

주의: 계정 하나만 보이는 토큰이 실제로 있었다. 재발급 뒤에는 반드시 `--check` 로
메인·서브가 모두 보이는지 확인하고 config.json 의 kakao_token 에 넣는다.

    # 1) 지금 토큰 점검
    python issue_token.py --check

    # 2) 인가 주소 출력 (브라우저에서 열어 로그인·동의)
    python issue_token.py url --rest-key REST키 --redirect-uri URI --scope moment_management

    # 3) 되돌아온 주소 전체를 따옴표로 감싸 전달
    python issue_token.py exchange --rest-key REST키 --redirect-uri URI \
        --client-secret 시크릿 --redirected "https://.../?code=..."

    --client-secret 은 앱 [보안] 탭에서 비활성화 상태면 생략.
    발급된 토큰은 화면에 찍히고 config.json 의 kakao_token 에 바로 써 넣는다
    (config.json 은 깃에 올라가지 않는다).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
MOMENT = "https://apis.moment.kakao.com/openapi/v4"


def build_urls(rest_key, redirect_uri, scope="", resource_ids=""):
    enc = urllib.parse.quote(redirect_uri, safe="")
    scope_q = f"&scope={urllib.parse.quote(scope, safe='')}" if scope else ""
    res_q = (f"&resource_ids={urllib.parse.quote(resource_ids, safe=':*,')}"
             if resource_ids else "")
    return {
        "business": ("https://kauth.kakao.com/oauth/business/authorize"
                     f"?client_id={rest_key}&redirect_uri={enc}"
                     f"&response_type=code{res_q}{scope_q}"),
        "standard": ("https://kauth.kakao.com/oauth/authorize"
                     f"?client_id={rest_key}&redirect_uri={enc}"
                     f"&response_type=code{res_q}{scope_q}"),
    }


def extract_code(redirected: str) -> str:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query)
    if "error" in qs:
        print(f"[오류] 인가 단계 오류 응답: {dict(qs)}")
        print("-> 로그인한 카카오계정이 그 광고계정의 멤버인지 확인하세요")
        sys.exit(1)
    return qs.get("code", [""])[0] or redirected.strip()


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check(token: str | None = None) -> int:
    """토큰이 어느 광고계정까지 보이는지. 만료 여부가 아니라 '권한 범위' 점검이다."""
    tok = token or read_config().get("kakao_token")
    if not tok:
        print("[!] config.json 에 kakao_token 이 없습니다.")
        return 1
    r = requests.get(f"{MOMENT}/adAccounts",
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    print(f"HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300])
        print("-> 401 이면 토큰 값이 잘렸거나 권한이 바뀐 경우입니다 (만료는 아닙니다).")
        return 1
    rows = r.json().get("content") or []
    print(f"보이는 광고계정 {len(rows)}개:")
    for a in rows:
        print(f"  {a.get('id')}  {a.get('name')}  ({a.get('memberType')}, {a.get('config')})")
    원하는 = (read_config().get("kakao_accounts")
             or {"카카오 메인": 802036, "카카오 서브": 928346})
    보임 = {a.get("id") for a in rows}
    빠짐 = {k: v for k, v in 원하는.items() if int(v) not in 보임}
    if 빠짐:
        print("\n[!] 대시보드가 쓰는 계정 중 안 보이는 것: "
              + ", ".join(f"{k}({v})" for k, v in 빠짐.items()))
        print("    이 토큰으로는 그 계정 화면이 비어 보입니다. 재발급이 필요합니다.")
        return 1
    print("\n대시보드가 쓰는 계정이 모두 보입니다.")
    return 0


def save_token(tok: str) -> None:
    cfg = read_config()
    cfg["kakao_token"] = tok
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"config.json 의 kakao_token 에 저장했습니다 ({CONFIG})")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", nargs="?", choices=["url", "exchange"])
    p.add_argument("--check", action="store_true", help="지금 토큰의 광고계정 권한 점검")
    p.add_argument("--rest-key", default="")
    p.add_argument("--redirect-uri", default="")
    p.add_argument("--client-secret", default="")
    p.add_argument("--redirected", default="")
    p.add_argument("--auth-type", choices=["business", "standard"], default="business")
    p.add_argument("--scope", default="moment_management")
    p.add_argument("--resource-ids", default="")
    args = p.parse_args()

    if args.check or not args.mode:
        return check()

    if not args.rest_key or not args.redirect_uri:
        print("--rest-key 와 --redirect-uri 가 필요합니다")
        return 1

    if args.mode == "url":
        urls = build_urls(args.rest_key, args.redirect_uri, args.scope, args.resource_ids)
        print("[1번 주소 - 비즈니스 인증] (우선 시도)")
        print(urls["business"])
        print()
        print("[2번 주소 - 일반 인증] (1번이 오류 페이지면 시도)")
        print(urls["standard"])
        print()
        print("로그인·동의 뒤 주소창의 주소 '전체' 를 복사해 exchange 로 넘기세요.")
        return 0

    if not args.redirected:
        print("--redirected 에 되돌아온 주소를 전달하세요")
        return 1

    token_url = ("https://kauth.kakao.com/oauth/business/token"
                 if args.auth_type == "business" else "https://kauth.kakao.com/oauth/token")
    data = {"grant_type": "authorization_code", "client_id": args.rest_key,
            "redirect_uri": args.redirect_uri, "code": extract_code(args.redirected)}
    if args.client_secret:
        data["client_secret"] = args.client_secret

    r = requests.post(token_url, data=data, timeout=30)
    try:
        j = r.json()
    except Exception:
        print(f"HTTP {r.status_code}\n{r.text[:400]}")
        return 1
    if not (r.ok and "access_token" in j):
        print(f"[발급 실패] HTTP {r.status_code}")
        print(json.dumps(j, ensure_ascii=False, indent=2))
        print("(인가 코드는 1회용 + 유효시간이 짧습니다 -> url 단계부터 다시)")
        return 1

    print("발급 성공")
    print(f"  access_token  : {j['access_token']}")
    print(f"  expires_in    : {j.get('expires_in', '(없음 - 만료되지 않는 토큰)')}")
    print(f"  refresh_token : {j.get('refresh_token', '(없음)')}")
    print(f"  scope         : {j.get('scope')}")
    save_token(j["access_token"])
    print()
    print("이어서 권한 범위를 점검합니다.")
    return check(j["access_token"])


if __name__ == "__main__":
    sys.exit(main())
