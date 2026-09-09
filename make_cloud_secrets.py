"""service_account.json + config.json 으로 Streamlit Cloud 용 secrets 를 만들어 준다.

    python make_cloud_secrets.py "팀비밀번호"

출력된 내용을 Streamlit Cloud 앱의 Settings > Secrets 칸에 그대로 붙여넣으면 된다.
private_key 줄바꿈 처리를 대신하므로 손으로 복사하다 깨질 일이 없다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
KEYS = ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id",
        "auth_uri", "token_uri", "auth_provider_x509_cert_url", "client_x509_cert_url"]


def q(v) -> str:
    return json.dumps(str(v), ensure_ascii=False)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    sa_path = HERE / "service_account.json"
    if not sa_path.exists():
        print("service_account.json 이 없습니다.", file=sys.stderr)
        return 1
    sa = json.loads(sa_path.read_text(encoding="utf-8"))
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8")) \
        if (HERE / "config.json").exists() else {}

    ad_id = (cfg.get("ad_sheet_id") or "").strip()
    if not ad_id:
        print("[!] config.json 의 ad_sheet_id 가 비어 있습니다. 광고 시트를 먼저 연결하세요.",
              file=sys.stderr)

    pw = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else "팀비밀번호"
    out = [
        f"app_password = {q(pw)}",
        f"ad_sheet_id = {q(ad_id)}",
        f"ad_worksheet = {q(cfg.get('ad_worksheet', 'KakaopayRAW'))}",
        'db_sheet_id = "1BTfbVKKCbe-6g2x3SQilnFAILMXB-Yj0C4GLG77o1r4"',
        "",
        "[gcp_service_account]",
    ] + [f"{k} = {q(sa[k])}" for k in KEYS if k in sa]

    print("---- 아래 내용을 Streamlit Cloud > Settings > Secrets 에 붙여넣으세요 ----")
    print("\n".join(out))
    print("---- 여기까지 ----")
    return 0


if __name__ == "__main__":
    sys.exit(main())
