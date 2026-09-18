"""특정 날짜·소재의 전환수를 손으로 바로잡는다.

왜 필요한가
    랜딩에서 utm_content 가 빠지거나 잘못 찍히는 날이 있다. 그러면 그 소재의 전환이
    시트에서 0건으로 잡혀 전환단가가 실제와 달라진다. 사이드바의 '전환수 직접 지정' 은
    타이핑한 그 순간에만 적용되고 날짜 구분도 없어서, 지난 날짜를 영구히 고치기엔 맞지 않는다.

어떻게 고치나
    집계식을 건드리지 않고 **원본 전환 목록 자체**를 맞춰 준다.
    모자라면 그 소재의 전환 행을 그만큼 만들어 넣고, 넘치면 뒤에서부터 덜어 낸다.
    이렇게 하면 소재별 표·세트별 표·일별 추이·합계가 모두 저절로 같은 값을 쓴다.

    conversion_fixes.json
        { "2026-09-18": { "채무조정_ad3": 3, "채무조정_ad5": 3 } }
        날짜 → 소재명 → 그 날 그 소재의 전환수(최종값). 적힌 것만 손대고 나머지는 그대로 둔다.

    소재명 → utm_content 는 대시보드의 기존 규칙을 그대로 쓴다.
        채무조정_ad3 (세트1 3번) → kakaopay_ad1-3
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from core.metrics import creative_key

HERE = Path(__file__).resolve().parents[1]
FIXES_PATH = HERE / "conversion_fixes.json"


def load(path: Path | None = None) -> dict[str, dict[str, int]]:
    """{ '2026-09-18': {'채무조정_ad3': 3} }. 파일이 없거나 깨졌으면 빈 값."""
    p = path or FIXES_PATH
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for day, table in raw.items():
        if day.startswith("_") or not isinstance(table, dict):
            continue          # '_설명' 같은 메모 줄은 건너뛴다
        out[day] = {k: int(v) for k, v in table.items()}
    return out


def utm_of(creative: str) -> str | None:
    """'채무조정_ad3' → 'kakaopay_ad1-3' (세트번호 없는 이름은 세트1)"""
    k = creative_key(creative)
    return f"kakaopay_ad{k[0]}-{k[1]}" if k else None


def apply(db: pd.DataFrame, fixes: dict[str, dict[str, int]] | None = None) -> pd.DataFrame:
    """전환 목록을 보정값에 맞춘다. 건드리지 않은 날짜·소재는 그대로."""
    fixes = load() if fixes is None else fixes
    if db is None or db.empty or not fixes:
        return db

    out = db.copy()
    for day_s, table in fixes.items():
        try:
            day = dt.date.fromisoformat(day_s)
        except ValueError:
            continue
        for creative, want in table.items():
            utm = utm_of(creative)
            if not utm:
                continue
            해당 = out[(out["날짜"] == day) & (out["utm_content"].astype(str).str.lower() == utm)]
            지금 = len(해당)
            if 지금 == want:
                continue
            if 지금 > want:                      # 넘치면 뒤에서부터 덜어 낸다
                out = out.drop(해당.index[want:])
            else:                                # 모자라면 그만큼 만들어 넣는다
                본 = 해당.iloc[0].to_dict() if 지금 else {}
                새행 = {c: 본.get(c, 0 if c in ("진행불가", "접수", "미팅", "승인") else "")
                       for c in out.columns}
                새행.update({"날짜": day, "utm_content": utm, "utm_source": "kakaopay",
                            "구분": 본.get("구분", "미분류")})
                out = pd.concat([out, pd.DataFrame([새행] * (want - 지금))], ignore_index=True)
    return out.reset_index(drop=True)


def describe(fixes: dict[str, dict[str, int]] | None = None) -> str:
    """화면에 보여 줄 한 줄 설명."""
    fixes = load() if fixes is None else fixes
    if not fixes:
        return ""
    항목 = [f"{day} {c} {n}건" for day, t in sorted(fixes.items()) for c, n in sorted(t.items())]
    return "손으로 바로잡은 전환수: " + " · ".join(항목)
