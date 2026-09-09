"""매칭·계산 규칙 (지시서 2절). 순수 함수 - Streamlit/시트에 의존하지 않는다."""
from __future__ import annotations

import re

import pandas as pd

BUCKETS = ["진행불가", "접수", "미팅", "승인"]

# 채무조정3_ad2 → 접두어 '채무조정', 세트 3, 소재 2 / 세트번호 없으면 세트1
RX_CREATIVE = re.compile(r"^(.*?)(\d*)_ad(\d+)$")
# kakaopay_ad3-2 → 세트 3, 소재 2
RX_CONTENT = re.compile(r"kakaopay_ad(\d+)-(\d+)", re.I)


def creative_key(name) -> tuple[int, int] | None:
    """광고 소재명 → (세트번호, 소재번호). 규칙에 안 맞으면 None."""
    m = RX_CREATIVE.match(str(name or "").strip())
    if not m:
        return None
    return (int(m[2]) if m[2] else 1), int(m[3])


def content_key(utm_content) -> tuple[int, int] | None:
    """DB utm_content → (세트번호, 소재번호)."""
    m = RX_CONTENT.search(str(utm_content or ""))
    return (int(m[1]), int(m[2])) if m else None


def parse_pairs(text: str) -> dict[str, float]:
    """'채무조정_ad2 26400' 여러 줄 → {'채무조정_ad2': 26400.0}"""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        parts = line.replace(":", " ").replace("\t", " ").replace(",", "").split()
        if len(parts) >= 2:
            try:
                out[parts[0].strip()] = float(parts[1].replace("원", "").replace("건", ""))
            except ValueError:
                continue
    return out


def build_creative_table(ad: pd.DataFrame, db: pd.DataFrame,
                         deduct: dict[str, float] | None = None,
                         db_override: dict[str, float] | None = None,
                         exclude_groups=("테스트",)) -> pd.DataFrame:
    """소재별 집계표.

    ad: parsers 표준 스키마 (날짜/소재/광고그룹/소진비용/…)
    db: sources.db_sheet 표준 스키마 (날짜/utm_content/구분/…)
    """
    deduct = deduct or {}
    db_override = db_override or {}

    a = ad.copy()
    if exclude_groups:
        pat = "|".join(re.escape(x) for x in exclude_groups)
        a = a[~a["광고그룹"].astype(str).str.contains(pat, na=False)]

    # 소재명이 곧 소재의 정체다. 세트명은 바뀔 수 있으므로(예: '채무조정 세트' →
    # '채무조정 세트 / 07~24 / 납입금 절감') 소재명으로 묶고 가장 최근 세트명을 붙인다.
    # 세트로 묶으면 이름이 바뀐 날 기준으로 같은 소재가 두 행으로 갈라지고,
    # 중복키 규칙에 걸려 한쪽 DB 가 0이 되면서 미전환 소진이 부풀려진다.
    a = a.sort_values("날짜")
    g = (a.groupby("소재", as_index=False)
           .agg(광고그룹=("광고그룹", "last"), 소진=("소진비용", "sum"),
                노출=("노출수", "sum"), 클릭=("클릭수", "sum"),
                상태=("상태", "last"), ONOFF=("ON/OFF", "last")))
    g = g[["광고그룹", "소재", "소진", "노출", "클릭", "상태", "ONOFF"]]
    g["key"] = g["소재"].map(creative_key)
    g["세트번호"] = g["key"].map(lambda k: k[0] if k else None)

    # ---- DB 매칭
    d = db.copy()
    d["key"] = d["utm_content"].map(content_key)
    matched = d.dropna(subset=["key"])
    cnt = matched.groupby("key").size().to_dict()
    # 구분은 배타가 아니다(미팅확정은 접수에도 포함). 그래서 라벨로 세지 않고
    # 각 구분의 0/1 플래그를 더한다.
    sums = {b: (matched.groupby("key")[b].sum().to_dict() if b in matched else {})
            for b in BUCKETS}

    g["DB"] = g["key"].map(lambda k: int(cnt.get(k, 0)))
    for b in BUCKETS:
        g[b] = g["key"].map(lambda k, b=b: int(sums[b].get(k, 0)))

    # 같은 (세트, 번호) 소재가 둘 이상이면 소진이 큰 쪽에만 DB 배정
    if len(g):
        dup = g["key"].notna() & (g.groupby("key")["소재"].transform("size") > 1)
        if dup.any():
            rank = g.groupby("key")["소진"].rank(method="first", ascending=False)
            loser = dup & (rank > 1)
            for col in ["DB", *BUCKETS]:
                g.loc[loser, col] = 0

    # ---- 수동 보정
    g["DB"] = [int(db_override.get(n, v)) for n, v in zip(g["소재"], g["DB"])]
    g["차감"] = g["소재"].map(lambda n: float(deduct.get(n, 0.0)))
    g["최종소진"] = g["소진"] - g["차감"]
    g["DB단가"] = [round(s / n) if n > 0 else None for s, n in zip(g["최종소진"], g["DB"])]

    return g.sort_values(["광고그룹", "최종소진"], ascending=[True, False]).reset_index(drop=True)


def summarize(g: pd.DataFrame, deduct: dict[str, float] | None = None) -> dict:
    """합계 지표. 전체 DB단가는 총소진÷총DB (개별 단가 평균 금지)."""
    deduct = deduct or {}
    spend_raw = float(g["소진"].sum()) if len(g) else 0.0
    ded = float(g["차감"].sum()) if len(g) else 0.0
    spend = spend_raw - ded
    total_db = int(g["DB"].sum()) if len(g) else 0
    return {
        "총소진_차감전": spend_raw,
        "차감": ded,
        "최종소진": spend,
        "총DB": total_db,
        "DB단가": round(spend / total_db) if total_db else None,
        "미전환소재소진": float(g.loc[g["DB"] == 0, "최종소진"].sum()) if len(g) else 0.0,
        # 접수가 이미 미팅확정을 포함하므로 그대로가 '접수 이상' 이다
        "접수이상": int(g["접수"].sum()) if len(g) else 0,
        "클릭": float(g["클릭"].sum()) if len(g) else 0.0,
    }


def unmatched_db(g: pd.DataFrame, db: pd.DataFrame) -> pd.DataFrame:
    """광고 소재 목록에 없는 DB (utm_content 오타·삭제된 소재 등)."""
    d = db.copy()
    d["key"] = d["utm_content"].map(content_key)
    known = set(g["key"].dropna()) if len(g) else set()
    return d[~d["key"].isin(known)].reset_index(drop=True)


def report_text(g: pd.DataFrame, summary: dict, d0, d1=None,
                deduct: dict[str, float] | None = None) -> str:
    """채팅에 그대로 붙여넣는 복사용 리포트 (지시서 3절 형식)."""
    deduct = deduct or {}
    period = f"{d0}" + (f" ~ {d1}" if d1 and d1 != d0 else "")
    lines = [f"[소재별 DB 현황] — {period}", ""]
    for setname, part in g.groupby("광고그룹", sort=False):
        lines.append(f"■ {setname}")
        for _, r in part.iterrows():
            tag = f" ({r['상태']})" if r["상태"] and r["상태"] != "진행중" else ""
            lines.append(f"{r['소재']}{tag}")
            spend = f"- 소진: {r['최종소진']:,.0f}원"
            if r["차감"]:
                spend += f" (원 소진 {r['소진']:,.0f}원 − 차감 {r['차감']:,.0f}원)"
            lines.append(spend)
            lines.append(f"- DB: {int(r['DB'])}건")
            lines.append(f"- DB단가: {int(r['DB단가']):,}원" if pd.notna(r["DB단가"]) else "- DB단가: -")
            lines.append("")
    lines += ["[합계]",
              f"- 기존 총 소진: {summary['총소진_차감전']:,.0f}원",
              f"- CPC 수정 전 지출 차감: {summary['차감']:,.0f}원"]
    for n, v in deduct.items():
        lines.append(f"  · {n}: {v:,.0f}원")
    lines += [f"- 최종 소진: {summary['최종소진']:,.0f}원",
              f"- 총 DB: {summary['총DB']}건",
              f"- 최종 DB단가: {summary['DB단가']:,}원" if summary["DB단가"] else "- 최종 DB단가: -",
              f"- 미전환 소재 소진: {summary['미전환소재소진']:,.0f}원"]
    return "\n".join(lines)
