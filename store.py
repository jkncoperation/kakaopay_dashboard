"""로컬 저장소 (sqlite) - 날짜+광고그룹+소재 키로 upsert.

같은 날짜를 다시 수집하면 그 날짜 행만 갈아끼운다. 일별/기간 조회의 근거가 되는 곳.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

from parsers.adcenter_file import AD_COLUMNS

DB_PATH = Path(__file__).with_name("data") / "ad_daily.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ad_daily (
    날짜      TEXT NOT NULL,
    광고그룹  TEXT NOT NULL,
    소재      TEXT NOT NULL,
    "ON/OFF"  TEXT,
    상태      TEXT,
    광고상품  TEXT,
    소진비용  REAL,
    노출수    REAL,
    클릭수    REAL,
    클릭률    REAL,
    도달수    REAL,
    eCPM      REAL,
    CPC       REAL,
    시작일    TEXT,
    종료일    TEXT,
    출처      TEXT,
    수집시각  TEXT,
    PRIMARY KEY (날짜, 광고그룹, 소재)
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    return con


def upsert_ad(df: pd.DataFrame, path: Path | str = DB_PATH) -> int:
    """표준 스키마 DataFrame 을 저장. 같은 (날짜, 광고그룹, 소재)는 덮어쓴다."""
    if df is None or df.empty:
        return 0
    d = df.copy()
    for c in AD_COLUMNS:
        if c not in d:
            d[c] = "" if c in ("ON/OFF", "상태", "광고상품", "시작일", "종료일", "출처", "수집시각") else 0.0
    d = d[AD_COLUMNS]
    d["날짜"] = d["날짜"].map(lambda v: v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else str(v))
    d = d[d["날짜"].notna() & d["날짜"].ne("None") & d["날짜"].ne("")]
    if d.empty:
        return 0
    # 같은 파일 안에서 (날짜, 그룹, 소재)가 중복이면 마지막 것만
    d = d.drop_duplicates(subset=["날짜", "광고그룹", "소재"], keep="last")

    cols = ", ".join(f'"{c}"' for c in AD_COLUMNS)
    ph = ", ".join("?" for _ in AD_COLUMNS)
    with connect(path) as con:
        con.executemany(f"INSERT OR REPLACE INTO ad_daily ({cols}) VALUES ({ph})",
                        d.itertuples(index=False, name=None))
    return len(d)


def load_ad(start=None, end=None, path: Path | str = DB_PATH) -> pd.DataFrame:
    """기간 조회. start/end 는 date 또는 'YYYY-MM-DD'."""
    q, args = "SELECT * FROM ad_daily", []
    conds = []
    if start is not None:
        conds.append("날짜 >= ?")
        args.append(str(start))
    if end is not None:
        conds.append("날짜 <= ?")
        args.append(str(end))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    with connect(path) as con:
        df = pd.read_sql_query(q, con, params=args)
    if df.empty:
        return pd.DataFrame(columns=AD_COLUMNS)
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    return df


def available_dates(path: Path | str = DB_PATH) -> list[str]:
    with connect(path) as con:
        rows = con.execute("SELECT DISTINCT 날짜 FROM ad_daily ORDER BY 날짜 DESC").fetchall()
    return [r[0] for r in rows]


def last_collected_at(date=None, path: Path | str = DB_PATH) -> str | None:
    """해당 날짜(없으면 전체)의 마지막 수집시각 - '오늘' 실시간 값의 기준 시각."""
    q = "SELECT MAX(수집시각) FROM ad_daily"
    args: list = []
    if date is not None:
        q += " WHERE 날짜 = ?"
        args.append(str(date))
    with connect(path) as con:
        v = con.execute(q, args).fetchone()[0]
    return v or None


def delete_date(date, path: Path | str = DB_PATH) -> int:
    with connect(path) as con:
        cur = con.execute("DELETE FROM ad_daily WHERE 날짜 = ?", (str(date),))
        return cur.rowcount


# ------------------------------------------------------------------ 전환(DB) 행
# DB 시트는 전체 스냅샷이므로 통째로 갈아끼운다(부분 갱신 개념이 없음).
_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS db_rows (
    날짜 TEXT, utm_source TEXT, utm_campaign TEXT, utm_content TEXT,
    접수 TEXT, 승인 TEXT, 구분 TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

DB_COLS = ["날짜", "utm_source", "utm_campaign", "utm_content", "접수", "승인", "구분"]


def save_db(df: pd.DataFrame, path: Path | str = DB_PATH) -> int:
    """전환 시트 스냅샷을 통째로 저장(기존 내용 대체)."""
    with connect(path) as con:
        con.executescript(_DB_SCHEMA)
        con.execute("DELETE FROM db_rows")
        if df is not None and not df.empty:
            d = df.copy()
            for c in DB_COLS:
                if c not in d:
                    d[c] = ""
            d = d[DB_COLS]
            d["날짜"] = d["날짜"].map(
                lambda v: v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else ("" if v is None else str(v)))
            con.executemany(f"INSERT INTO db_rows ({', '.join(DB_COLS)}) "
                            f"VALUES ({', '.join('?' for _ in DB_COLS)})",
                            d.itertuples(index=False, name=None))
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('db_saved_at', ?)",
                    (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),))
    return 0 if df is None or df.empty else len(df)


def load_db(start=None, end=None, path: Path | str = DB_PATH) -> pd.DataFrame:
    with connect(path) as con:
        con.executescript(_DB_SCHEMA)
        df = pd.read_sql_query("SELECT * FROM db_rows", con)
    if df.empty:
        return pd.DataFrame(columns=DB_COLS)
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    if start is not None:
        df = df[df["날짜"].notna() & (df["날짜"] >= start)]
    if end is not None:
        df = df[df["날짜"].notna() & (df["날짜"] <= end)]
    return df.reset_index(drop=True)


def db_saved_at(path: Path | str = DB_PATH) -> str | None:
    with connect(path) as con:
        con.executescript(_DB_SCHEMA)
        row = con.execute("SELECT v FROM meta WHERE k = 'db_saved_at'").fetchone()
    return row[0] if row else None
