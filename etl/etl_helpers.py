"""
etl_helpers.py
==============
Fungsi bersama untuk semua modul ETL Nasmoco Tegal.
"""

import re
import sqlite3
import warnings
import pandas as pd
from glob import glob
from pathlib import Path

warnings.filterwarnings('ignore')

# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════

BASE    = r"D:\Database Unit Masuk"
AI_BASE = r"D:\AI_nasmoco"

PATHS = {
    "unit_masuk":     BASE + r"\Unit Masuk Harian",
    "invoice":        BASE + r"\Invoice",
    "parts_baru":     BASE + r"\Parts\baru",
    "parts_cache":    BASE + r"\Parts\cache",
    "master":         BASE + r"\Master",
    "rs":             BASE + r"\RS",
    "tcare_nasional": BASE + r"\TCARE\TCARE Nasional",
    "mapping_cust":   BASE + r"\TCARE\Mapping Cust",
    "output":         AI_BASE + r"\db",
}

DB_PATH = str(Path(PATHS["output"]) / "nasmoco.db")


# ════════════════════════════════════════
# FILE HELPERS
# ════════════════════════════════════════

def get_excel_files(folder: str) -> list:
    files = []
    for ext in ('*.xls', '*.xlsx', '*.xlsm'):
        files += glob(f"{folder}/**/{ext}", recursive=True)
    return sorted(f for f in set(files)
                  if not Path(f).name.startswith('~$'))

def s(v):
    return str(v).strip() if not pd.isna(v) else ''

def n(v):
    return v if not pd.isna(v) else 0


# ════════════════════════════════════════
# DATE HELPERS
# ════════════════════════════════════════

def parse_date_flexible(val) -> str:
    """Konversi berbagai format tanggal ke YYYY-MM-DD string."""
    if pd.isna(val) or val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            d = pd.Timestamp('1899-12-30') + pd.Timedelta(days=int(val))
            return d.strftime('%Y-%m-%d')
        except Exception:
            return None
    try:
        return pd.to_datetime(val, dayfirst=True).strftime('%Y-%m-%d')
    except Exception:
        return None


# ════════════════════════════════════════
# ADDRESS PARSER
# ════════════════════════════════════════

def parse_alamat(alamat: str) -> tuple:
    """
    Extract kecamatan dan kabupaten dari Alamat STNK.
    Return: (kecamatan, kabupaten) atau (None, None).
    """
    if not alamat or pd.isna(alamat):
        return None, None
    alamat = str(alamat).upper().strip()
    kec = None
    m = re.search(r'KEC\.\s+([A-Z0-9 ]+?)(?:\s+(?:KAB\.|KOTA)|$)', alamat)
    if m:
        kec = m.group(1).strip()
    kab = None
    m = re.search(r'(KAB\.|KOTA)\s+([A-Z0-9 ]+?)(?:\s+\d|$)', alamat)
    if m:
        kab = f"{m.group(1).strip()} {m.group(2).strip()}"
    return kec, kab


def clean_no_rangka(s_val) -> str:
    """Bersihkan no_rangka — hapus titik di awal, strip whitespace."""
    if pd.isna(s_val):
        return None
    cleaned = str(s_val).strip().lstrip('.').strip()
    return cleaned if len(cleaned) > 5 else None


# ════════════════════════════════════════
# SQLITE SMART UPDATE
# ════════════════════════════════════════

def get_months_in_db(conn, table: str, date_col: str) -> set:
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT
                CAST(strftime('%Y',[{date_col}]) AS INTEGER),
                CAST(strftime('%m',[{date_col}]) AS INTEGER)
            FROM [{table}]
            WHERE [{date_col}] IS NOT NULL
        """).fetchall()
        return {(y, m) for y, m in rows}
    except Exception:
        return set()

def get_months_in_df(df: pd.DataFrame, date_col: str) -> set:
    col = pd.to_datetime(df[date_col], errors='coerce').dropna()
    return {(d.year, d.month) for d in col}

def replace_months(conn, table: str, df: pd.DataFrame,
                   date_col: str, months: set) -> int:
    if not months:
        return 0
    exists = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
    ).fetchone()
    if not exists:
        df.head(0).to_sql(table, conn, if_exists='replace', index=False)
        conn.commit()
    for year, month in months:
        conn.execute(f"""
            DELETE FROM [{table}]
            WHERE CAST(strftime('%Y',[{date_col}]) AS INTEGER)={year}
            AND   CAST(strftime('%m',[{date_col}]) AS INTEGER)={month}
        """)
    dates = pd.to_datetime(df[date_col], errors='coerce')
    mask  = dates.apply(
        lambda d: (d.year, d.month) in months if pd.notna(d) else False)
    inserted = int(mask.sum())
    if inserted > 0:
        df[mask].to_sql(table, conn, if_exists='append', index=False)
    conn.commit()
    return inserted

def append_new_months(conn, table: str, df: pd.DataFrame, date_col: str) -> int:
    existing   = get_months_in_db(conn, table, date_col)
    new_months = get_months_in_df(df, date_col) - existing
    if not new_months:
        return 0
    return replace_months(conn, table, df, date_col, new_months)
