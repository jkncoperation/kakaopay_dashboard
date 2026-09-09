"""매칭·계산 규칙. 순수 함수 - Streamlit/시트에 의존하지 않는다."""
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
    """전환 utm_content → (세트번호, 소재번호)."""
    m = RX_CONTENT.search(str(utm_content or ""))
    return (int(m[1]), int(m[2])) if m else None


def parse_pairs(text: str) -> dict[str, float]:
    """'채무조정_ad6 12' 여러 줄 → {'채무조정_ad6': 12.0}"""
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
                         db_override: dict[str, float] | None = None,
                         exclude_groups=("테스트",)) -> pd.DataFrame:
    """소재별 집계표.

    ad: parsers 표준 스키마 (날짜/소재/광고그룹/소진비용/…)
    db: sources.db_sheet 표준 스키마 (날짜/utm_content/진행불가·접수·미팅·승인 플래그)
    """
    db_override = db_override or {}

    a = ad.copy()
    if exclude_groups:
        pat = "|".join(re.escape(x) for x in exclude_groups)
        a = a[~a["광고그룹"].astype(str).str.contains(pat, na=False)]

    # 소재명이 곧 소재의 정체다. 세트명은 바뀔 수 있으므로(예: '채무조정 세트' →
    # '채무조정 세트 / 07~24 / 납입금 절감') 소재명으로 묶고 가장 최근 세트명을 붙인다.
    # 세트로 묶으면 이름이 바뀐 날 기준으로 같은 소재가 두 행으로 갈라지고,
    # 중복키 규칙에 걸려 한쪽 전환수가 0이 되면서 미전환 지출이 부풀려진다.
    a = a.sort_values("날짜")
    g = (a.groupby("소재", as_index=False)
           .agg(광고그룹=("광고그룹", "last"), 지출=("소진비용", "sum"),
                노출=("노출수", "sum"), 클릭=("클릭수", "sum"),
                상태=("상태", "last"), ONOFF=("ON/OFF", "last")))
    g = g[["광고그룹", "소재", "지출", "노출", "클릭", "상태", "ONOFF"]]
    g["key"] = g["소재"].map(creative_key)
    g["세트번호"] = g["key"].map(lambda k: k[0] if k else None)

    # ---- 전환 매칭
    d = db.copy()
    d["key"] = d["utm_content"].map(content_key)
    matched = d.dropna(subset=["key"])
    cnt = matched.groupby("key").size().to_dict()
    # 구분은 배타가 아니다(미팅확정은 접수에도 포함). 그래서 라벨로 세지 않고
    # 각 구분의 0/1 플래그를 더한다.
    sums = {b: (matched.groupby("key")[b].sum().to_dict() if b in matched else {})
            for b in BUCKETS}

    g["전환수"] = g["key"].map(lambda k: int(cnt.get(k, 0)))
    for b in BUCKETS:
        g[b] = g["key"].map(lambda k, b=b: int(sums[b].get(k, 0)))

    # 같은 (세트, 번호) 소재가 둘 이상이면 지출이 큰 쪽에만 전환을 배정
    if len(g):
        dup = g["key"].notna() & (g.groupby("key")["소재"].transform("size") > 1)
        if dup.any():
            rank = g.groupby("key")["지출"].rank(method="first", ascending=False)
            loser = dup & (rank > 1)
            for col in ["전환수", *BUCKETS]:
                g.loc[loser, col] = 0

    # ---- 수동 보정 (시트 집계보다 우선)
    g["전환수"] = [int(db_override.get(n, v)) for n, v in zip(g["소재"], g["전환수"])]
    g["전환단가"] = [round(s / n) if n > 0 else None for s, n in zip(g["지출"], g["전환수"])]

    return g.sort_values(["광고그룹", "지출"], ascending=[True, False]).reset_index(drop=True)


def summarize(g: pd.DataFrame) -> dict:
    """합계 지표. 전체 전환단가는 총지출÷총전환수 (개별 단가 평균 금지)."""
    spend = float(g["지출"].sum()) if len(g) else 0.0
    total = int(g["전환수"].sum()) if len(g) else 0
    return {
        "지출": spend,
        "총전환수": total,
        "전환단가": round(spend / total) if total else None,
        "미전환소재지출": float(g.loc[g["전환수"] == 0, "지출"].sum()) if len(g) else 0.0,
        # 접수가 이미 미팅확정을 포함하므로 그대로가 '접수 이상' 이다
        "접수이상": int(g["접수"].sum()) if len(g) else 0,
        "클릭": float(g["클릭"].sum()) if len(g) else 0.0,
    }


def unmatched_db(g: pd.DataFrame, db: pd.DataFrame) -> pd.DataFrame:
    """광고 소재 목록에 없는 전환 (utm_content 오타·삭제된 소재 등)."""
    d = db.copy()
    d["key"] = d["utm_content"].map(content_key)
    known = set(g["key"].dropna()) if len(g) else set()
    return d[~d["key"].isin(known)].reset_index(drop=True)


def report_text(g: pd.DataFrame, summary: dict, d0, d1=None) -> str:
    """채팅에 그대로 붙여넣는 복사용 리포트."""
    period = f"{d0}" + (f" ~ {d1}" if d1 and d1 != d0 else "")
    lines = [f"[소재별 전환 현황] — {period}", ""]
    for setname, part in g.groupby("광고그룹", sort=False):
        lines.append(f"■ {setname}")
        for _, r in part.iterrows():
            tag = f" ({r['상태']})" if r["상태"] and r["상태"] != "진행중" else ""
            lines.append(f"{r['소재']}{tag}")
            lines.append(f"- 지출: {r['지출']:,.0f}원")
            lines.append(f"- 전환수: {int(r['전환수'])}건")
            lines.append(f"- 전환단가: {int(r['전환단가']):,}원"
                         if pd.notna(r["전환단가"]) else "- 전환단가: -")
            lines.append("")
    lines += ["[합계]",
              f"- 총 지출: {summary['지출']:,.0f}원",
              f"- 총 전환수: {summary['총전환수']}건",
              f"- 전환단가: {summary['전환단가']:,}원" if summary["전환단가"] else "- 전환단가: -",
              f"- 미전환 소재 지출: {summary['미전환소재지출']:,.0f}원"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 표시용 지표
def rate(part, whole, digits: int = 1) -> str:
    return f"{part / whole:.{digits}%}" if whole else "-"


def stage_cell(count: int, total: int, spend: float, with_price: bool = True) -> str:
    """상태 단계를 '건수 / 비율 / 영업단가' 한 칸으로.

    영업단가 = 지출 / 그 단계 건수. 진행불가는 단가가 의미 없어 건수/비율만.
    """
    n = int(count or 0)
    if n == 0:
        return "0"
    cell = f"{n} / {rate(n, total)}"
    if with_price:
        cell += f" / {round(spend / n):,}" if spend else " / -"
    return cell


def ad_metrics(spend: float, impressions: float, clicks: float, conversions: int) -> dict:
    """광고 효율 지표. CPM = 지출/노출*1000, CTR = 클릭/노출, 제출율 = 전환/클릭."""
    return {
        "CPM": round(spend / impressions * 1000) if impressions else None,
        # CTR 은 0.2% 대라 소수 한 자리면 자릿수를 잃는다
        "CTR": rate(clicks, impressions, digits=2),
        "제출율": rate(conversions, clicks),
    }
