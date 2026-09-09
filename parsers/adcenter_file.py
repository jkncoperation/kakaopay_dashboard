"""광고센터 '소재' 다운로드 파일(xlsx/csv) 파서.

실제 파일 헤더 (2026-09 기준):
    소재 | ON/OFF | 상태 | 광고그룹 | 광고상품 | 소진비용 | 노출수 | 클릭수 |
    클릭률 | 도달수 | eCPM | CPC | 시작일 | 종료일

열 순서가 바뀌거나 이름이 조금 달라도 헤더 이름으로 찾는다.
날짜는 파일 안에 없으므로 파일명(`..._20260909_20260909.xlsx`)에서 뽑고,
그마저 없으면 호출자가 지정한다.
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pandas as pd

from core.util import dates_from_filename, norm_header, to_num, to_ratio

# 표준 스키마 - 수집 경로(파일/실시간/캡처)가 달라도 항상 이 열로 맞춘다
AD_COLUMNS = ["날짜", "소재", "ON/OFF", "상태", "광고그룹", "광고상품",
              "소진비용", "노출수", "클릭수", "클릭률", "도달수", "eCPM", "CPC",
              "시작일", "종료일", "출처", "수집시각"]

NUM_COLS = ["소진비용", "노출수", "클릭수", "도달수", "eCPM", "CPC"]

# 표준열 → 허용 헤더(정규화 후)
ALIASES: dict[str, list[str]] = {
    "소재": ["소재", "소재명"],
    "ON/OFF": ["onoff", "on", "off", "운영", "사용여부"],
    "상태": ["상태", "심사상태", "운영상태"],
    "광고그룹": ["광고그룹", "광고그룹명", "상위광고그룹이름", "상위광고그룹", "광고세트", "세트"],
    "광고상품": ["광고상품", "상품", "광고유형"],
    "소진비용": ["소진비용", "비용", "광고비", "집행금액", "소진"],
    "노출수": ["노출수", "노출"],
    "클릭수": ["클릭수", "클릭"],
    "클릭률": ["클릭률", "ctr"],
    "도달수": ["도달수", "도달"],
    "eCPM": ["ecpm"],
    "CPC": ["cpc"],
    "시작일": ["시작일", "게재시작일", "시작"],
    "종료일": ["종료일", "게재종료일", "종료"],
    "기간": ["기간", "게재기간"],          # 화면 표는 '2026-09-09 ~ 2026-12-31' 한 칸
}


class ParseError(Exception):
    pass


def _read_raw(src, filename: str | None) -> pd.DataFrame:
    """xlsx / csv 를 헤더 있는 DataFrame 으로 읽는다. src 는 경로 또는 file-like."""
    name = (filename or (src if isinstance(src, (str, Path)) else "") or "")
    ext = Path(str(name)).suffix.lower()
    data = src

    if ext in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(data, dtype=object)
    if ext == ".csv":
        raw = Path(src).read_bytes() if isinstance(src, (str, Path)) else src.read()
        for enc in ("utf-8-sig", "cp949", "utf-8"):
            try:
                return pd.read_csv(io.BytesIO(raw), dtype=object, encoding=enc)
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        raise ParseError("CSV 인코딩을 알 수 없습니다 (utf-8/cp949 모두 실패).")

    # 확장자가 없으면 엑셀 → CSV 순으로 시도
    for reader in (lambda d: pd.read_excel(d, dtype=object),
                   lambda d: pd.read_csv(d, dtype=object)):
        try:
            if hasattr(data, "seek"):
                data.seek(0)
            return reader(data)
        except Exception:
            continue
    raise ParseError(f"열 수 없는 파일 형식입니다: {name}")


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    """실제 헤더 → 표준열 이름. 찾은 것만 담는다."""
    found: dict[str, str] = {}
    normed = {c: norm_header(c) for c in df.columns}
    for std, aliases in ALIASES.items():
        for col, n in normed.items():
            if n and n in aliases and std not in found:
                found[std] = col
                break
    return found


def parse_adcenter_file(src, filename: str | None = None,
                        date=None, 출처: str = "file") -> dict:
    """광고센터 파일 → {'df': 표준 DataFrame, 'start':, 'end':, 'warnings': [...]}

    date 를 주면 파일명보다 우선한다. 기간 파일(시작≠종료)이면 경고를 남긴다.
    """
    name = filename or (str(src) if isinstance(src, (str, Path)) else "")
    return normalize_raw(_read_raw(src, filename), filename=name, date=date, 출처=출처)


def normalize_raw(df: pd.DataFrame, filename: str | None = None,
                  date=None, 출처: str = "file") -> dict:
    """이미 읽어들인 표(DataFrame) → 표준 스키마.

    파일 경로와 실시간 화면 읽기 경로가 같은 정규화를 쓰도록 분리해 둔 지점.
    """
    if df is None or df.empty:
        raise ParseError("파일에 데이터 행이 없습니다.")

    colmap = _map_columns(df)
    if "소재" not in colmap or "소진비용" not in colmap:
        raise ParseError(
            "광고센터 소재 파일이 아닌 것 같습니다. "
            f"'소재'/'소진비용' 열을 찾지 못했습니다. 읽은 헤더: {list(df.columns)[:14]}")

    warnings: list[str] = []
    start = end = None
    if date is not None:
        start = end = date
    else:
        start, end = dates_from_filename(filename or "")
        if start is None:
            warnings.append("파일명에서 날짜를 찾지 못했습니다. 날짜를 직접 지정해 주세요.")
        elif start != end:
            warnings.append(
                f"이 파일은 {start} ~ {end} 합산입니다. 일별로 나눌 수 없어 시작일({start}) 하나로 저장합니다.")

    out = pd.DataFrame(index=df.index)
    out["날짜"] = start
    out["소재"] = df[colmap["소재"]].map(lambda v: str(v).strip() if v is not None else "")
    for std in ["ON/OFF", "상태", "광고그룹", "광고상품", "시작일", "종료일"]:
        out[std] = df[colmap[std]].map(lambda v: str(v).strip() if v is not None else "") \
            if std in colmap else ""
    for std in NUM_COLS:
        out[std] = df[colmap[std]].map(to_num) if std in colmap else 0.0
    out["클릭률"] = df[colmap["클릭률"]].map(to_ratio) if "클릭률" in colmap else 0.0

    # 화면 표는 시작·종료가 '기간' 한 칸에 들어온다 ('2026-09-09 ~ 2026-12-31')
    if "기간" in colmap and "시작일" not in colmap:
        parts = df[colmap["기간"]].map(lambda v: str(v or "").split("~"))
        out["시작일"] = parts.map(lambda x: x[0].strip() if x else "")
        out["종료일"] = parts.map(lambda x: x[1].strip() if len(x) > 1 else "")

    out["출처"] = 출처
    out["수집시각"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 합계행·빈 행 제거
    before = len(out)
    out = out[out["소재"].astype(str).str.strip().ne("")]
    out = out[~out["소재"].astype(str).str.strip().isin(["합계", "총계", "nan", "None"])]
    if len(out) < before:
        warnings.append(f"소재명이 비었거나 합계인 행 {before - len(out)}개를 건너뛰었습니다.")

    filled = set(colmap) | ({"시작일", "종료일"} if "기간" in colmap else set())
    missing = [c for c in ALIASES if c not in filled and c != "기간"]
    if missing:
        warnings.append(f"파일에 없는 열은 0/빈값으로 채웠습니다: {', '.join(missing)}")

    return {"df": out[AD_COLUMNS].reset_index(drop=True),
            "start": start, "end": end, "warnings": warnings}
