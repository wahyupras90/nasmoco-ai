"""
etl_rs.py
=========
ETL tabel rs — master unit (OWN + BERKAH).

Sumber:
  1. File RS Tegal (xlsx/xls)         → OWN units
  2. T-CARE Nasional (xlsx)            → tgl_do valid, tcare_type
  3. Mapping Cust (csv)                → BERKAH units, last_sbe
  4. unitmasuk (DB)                    → no_polisi valid

Kolom rs:
  no_rangka, customer, tgl_do, no_polisi, telp_gsm,
  nama_sales, batas_tcare, in_tcare, sisa_bulan_tcare,
  kecamatan, kabupaten,
  dealer_penjual, dealer_kategori   ← BARU
"""

import sqlite3
import pandas as pd
from pathlib import Path
from etl_helpers import (
    PATHS, DB_PATH, get_excel_files,
    parse_alamat, parse_date_flexible, clean_no_rangka, s
)

TCARE_NASIONAL_DIR = Path(PATHS["tcare_nasional"])
MAPPING_CUST_DIR   = Path(PATHS["mapping_cust"])
HEADER_KEYS        = {'No_Rangka', 'No. Rangka', 'Nama Sales', 'Nama_Sales',
                      'Tgl_DO', 'Tgl BPK'}


# ════════════════════════════════════════
# PARSE FILE RS TEGAL
# ════════════════════════════════════════

def _parse_rs_file(filepath: str) -> pd.DataFrame:
    try:
        df   = pd.read_excel(filepath, header=1, engine='openpyxl')
        cols = set(df.columns.astype(str).str.strip())
        if not HEADER_KEYS.intersection(cols):
            df = pd.read_excel(filepath, header=0, engine='openpyxl')
    except Exception:
        try:
            dfs = pd.read_html(filepath, header=1)
            df  = dfs[0]
        except Exception as e:
            raise ValueError(f"Tidak bisa baca file: {e}")

    cols = set(df.columns.astype(str).str.strip())

    if {'No_Rangka', 'Tgl_DO', 'Nama_Sales'}.issubset(cols):
        rename = {
            'No_Rangka':     'no_rangka',
            'Tgl_DO':        'tgl_do',
            'Nama_Sales':    'nama_sales',
            'No_Polisi':     'no_polisi',
            'Telp_GSM':      'telp_gsm',
            'Nama_Customer': 'customer',
            'Nama STNK':     'customer',
            'Alamat STNK':   'alamat_stnk',
        }
    elif {'No. Rangka', 'Tgl BPK', 'Nama Sales'}.issubset(cols):
        rename = {
            'No. Rangka':    'no_rangka',
            'Tgl BPK':       'tgl_do',
            'Nama Sales':    'nama_sales',
            'No. Polisi':    'no_polisi',
            'Telp GSM':      'telp_gsm',
            'Nama Customer': 'customer',
            'Nama STNK':     'customer',
            'Alamat STNK':   'alamat_stnk',
        }
    else:
        raise ValueError(f"Format tidak dikenali. Kolom: {cols}")

    keep = {k: v for k, v in rename.items() if k in cols}
    df   = df[list(keep.keys())].copy()
    df   = df.rename(columns=keep)

    # Hapus duplikat customer (rename ganda)
    if 'customer' in df.columns:
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

    return df


def load_rs_tegal(folder: str) -> pd.DataFrame:
    """Baca semua file RS Tegal → OWN units."""
    files = get_excel_files(folder)
    dfs = []
    for f in files:
        try:
            dfs.append(_parse_rs_file(f))
        except Exception as e:
            print(f"  ⚠ RS skip {Path(f).name}: {e}")
    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df['no_rangka'] = df['no_rangka'].apply(clean_no_rangka)
    df = df.dropna(subset=['no_rangka'])
    df['tgl_do']    = pd.to_datetime(df['tgl_do'], errors='coerce')
    df = (df.sort_values('tgl_do', ascending=False)
            .drop_duplicates(subset='no_rangka', keep='first')
            .reset_index(drop=True))
    df['dealer_penjual']   = 'NASMOCO TEGAL'
    df['dealer_kategori']  = 'OWN'
    df['tgl_do']           = df['tgl_do'].dt.strftime('%Y-%m-%d')
    return df


# ════════════════════════════════════════
# LOAD TCARE NASIONAL
# ════════════════════════════════════════

def load_tcare_nasional() -> pd.DataFrame:
    """Baca 2 file TCARE Nasional terbaru → tgl_do valid (Tgl DEC)."""
    files = sorted(TCARE_NASIONAL_DIR.glob('*.xlsx'), reverse=True)[:2]
    dfs = []
    for f in files:
        try:
            df = pd.read_excel(f, header=0, engine='openpyxl')
            df['_src'] = f.name
            dfs.append(df)
            print(f"  TCARE Nasional: {f.name} ({len(df):,} rows)")
        except Exception as e:
            print(f"  ⚠ Skip {f.name}: {e}")
    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df['no_rangka'] = df['No Rangka'].apply(clean_no_rangka)
    df = df.dropna(subset=['no_rangka'])
    df = df.drop_duplicates(subset=['no_rangka'], keep='first')

    result = pd.DataFrame({
        'no_rangka':         df['no_rangka'],
        'customer_tc':       df['Nama Customer'].str.strip(),
        'model_tc':          df['Model Kendaraan'].str.strip(),
        'tgl_do_tc':         df['Tgl DEC'].apply(parse_date_flexible),
        'dealer_penjual_tc': df.get('Dealer Penjual', pd.Series(dtype=str)),
    })
    print(f"  → TCARE Nasional: {len(result):,} unit unik")
    return result


# ════════════════════════════════════════
# LOAD MAPPING CUST
# ════════════════════════════════════════

def _process_mapping_cust(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Proses raw Mapping Cust DataFrame → result untuk rs table."""
    import time
    t = time.time()
    df = df_raw.copy()

    # Vectorized clean_no_rangka
    df['no_rangka'] = (df['No. CHASSIS']
                       .astype(str).str.strip().str.lstrip('.')
                       .str.strip())
    df = df[df['no_rangka'].str.len() > 5]
    df = df[df['no_rangka'] != 'nan']
    df = df.drop_duplicates(subset=['no_rangka'], keep='first')

    tgl_do_map = None
    if 'DELIVERY DATE' in df.columns:
        tgl_do_map = pd.to_datetime(
            df['DELIVERY DATE'], dayfirst=True, errors='coerce'
        ).dt.strftime('%Y-%m-%d')

    result = pd.DataFrame({
        'no_rangka':          df['no_rangka'].values,
        'customer_map':       df['NAMA CUSTOMER'].str.strip().values if 'NAMA CUSTOMER' in df.columns else None,
        'model_map':          df['NAMA MODEL'].str.strip().values    if 'NAMA MODEL'    in df.columns else None,
        'tgl_do_map':         tgl_do_map.values if tgl_do_map is not None else None,
        'dealer_penjual_map': df['Dealer Penjual'].values if 'Dealer Penjual' in df.columns else None,
    })
    return result


def load_mapping_cust_raw() -> pd.DataFrame:
    """Baca 2 file Mapping Cust terbaru → raw DataFrame (semua kolom asli).
    Dipakai untuk sharing antar modul tanpa baca file dua kali.
    """
    files = sorted(MAPPING_CUST_DIR.glob('*.csv'), reverse=True)[:2]
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f, sep=';', encoding='latin1', low_memory=False)
            df['_src'] = f.name
            dfs.append(df)
            print(f"  Mapping Cust: {f.name} ({len(df):,} rows)")
        except Exception as e:
            print(f"  ⚠ Skip {f.name}: {e}")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def load_mapping_cust() -> pd.DataFrame:
    """Baca 2 file Mapping Cust terbaru → BERKAH units."""
    df_raw = load_mapping_cust_raw()
    if len(df_raw) == 0:
        return pd.DataFrame()
    result = _process_mapping_cust(df_raw)
    print(f"  → Mapping Cust: {len(result):,} unit unik")
    return result


# ════════════════════════════════════════
# LOAD NO_POLISI VALID DARI UNITMASUK
# ════════════════════════════════════════

def load_nopol_from_unitmasuk() -> pd.DataFrame:
    """Ambil no_polisi terbaru per no_rangka dari unitmasuk.
    Menggunakan JOIN ke subquery MAX — lebih cepat dari correlated subquery.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT u.no_rangka, u.no_polisi
            FROM unitmasuk u
            INNER JOIN (
                SELECT no_rangka, MAX(tgl_invoice) AS max_inv
                FROM unitmasuk
                WHERE no_polisi IS NOT NULL AND TRIM(no_polisi) != ''
                  AND no_rangka IS NOT NULL AND TRIM(no_rangka) != ''
                GROUP BY no_rangka
            ) m ON u.no_rangka = m.no_rangka
              AND u.tgl_invoice = m.max_inv
              AND u.no_polisi IS NOT NULL AND TRIM(u.no_polisi) != ''
            GROUP BY u.no_rangka
        """, conn)
    except Exception:
        df = pd.DataFrame(columns=['no_rangka', 'no_polisi'])
    conn.close()
    return df.rename(columns={'no_polisi': 'no_polisi_um'})


# ════════════════════════════════════════
# BUILD RS TABLE
# ════════════════════════════════════════

def build_rs(folder: str, df_map_cache: pd.DataFrame = None) -> pd.DataFrame:
    """Gabungkan semua sumber menjadi master unit.
    
    df_map_cache: opsional, pass DataFrame Mapping Cust jika sudah dimuat
                  di luar (untuk menghindari baca file dua kali).
    """
    import time
    t = time.time()

    # 1. RS Tegal (OWN)
    df_own = load_rs_tegal(folder)
    print(f"  → OWN: {len(df_own):,} unit dari RS Tegal ({time.time()-t:.1f}s)"); t = time.time()

    # 2. TCARE Nasional
    df_tc = load_tcare_nasional()
    print(f"    tc_nasional   : {time.time()-t:.1f}s"); t = time.time()

    # 3. Mapping Cust (gunakan raw cache jika ada, proses jadi result)
    if df_map_cache is not None:
        df_map = _process_mapping_cust(df_map_cache)
        print(f"    mapping_cust  : dari cache ({time.time()-t:.1f}s)"); t = time.time()
    else:
        df_map = load_mapping_cust()
        print(f"    mapping_cust  : {time.time()-t:.1f}s"); t = time.time()

    # 4. No polisi valid dari unitmasuk
    df_nopol = load_nopol_from_unitmasuk()
    print(f"  → no_polisi valid dari unitmasuk: {len(df_nopol):,} ({time.time()-t:.1f}s)"); t = time.time()

    # ── Kumpulkan semua no_rangka ──
    all_nr = set()
    for df, col in [(df_own, 'no_rangka'), (df_tc, 'no_rangka'), (df_map, 'no_rangka')]:
        if len(df) > 0:
            all_nr.update(df[col].dropna().tolist())

    master = pd.DataFrame({'no_rangka': list(all_nr)})

    # ── Merge OWN ──
    if len(df_own) > 0:
        master = master.merge(df_own, on='no_rangka', how='left')

    # ── Merge TCARE Nasional ──
    if len(df_tc) > 0:
        master = master.merge(df_tc, on='no_rangka', how='left')

    # ── Merge Mapping Cust ──
    if len(df_map) > 0:
        master = master.merge(df_map, on='no_rangka', how='left')

    # ── Merge no_polisi valid ──
    if len(df_nopol) > 0:
        master = master.merge(df_nopol, on='no_rangka', how='left')

    # ── dealer_kategori ──
    master['dealer_kategori'] = master.get('dealer_kategori', pd.Series(dtype=str))
    master['dealer_kategori'] = master['dealer_kategori'].fillna('BERKAH')

    # ── dealer_penjual ──
    if 'dealer_penjual' not in master.columns:
        master['dealer_penjual'] = None
    master['dealer_penjual'] = master['dealer_penjual'].fillna(
        master.get('dealer_penjual_tc', pd.Series(dtype=str))
    ).fillna(master.get('dealer_penjual_map', pd.Series(dtype=str)))

    # ── customer ──
    if 'customer' not in master.columns:
        master['customer'] = None
    master['customer'] = master['customer'].fillna(
        master.get('customer_tc', pd.Series(dtype=str))
    ).fillna(master.get('customer_map', pd.Series(dtype=str)))

    # ── model ──
    master['model'] = master.get('model_tc', pd.Series(dtype=str)).fillna(
        master.get('model_map', pd.Series(dtype=str))
    )

    # ── tgl_do: Prioritas 1=Tgl DEC (TAM), 2=Mapping Cust, 3=RS Tegal ──
    if 'tgl_do' not in master.columns:
        master['tgl_do'] = None
    master['tgl_do'] = (
        master.get('tgl_do_tc',  pd.Series(dtype=str))
              .fillna(master.get('tgl_do_map', pd.Series(dtype=str)))
              .fillna(master.get('tgl_do',     pd.Series(dtype=str)))
    )

    # ── no_polisi: Prioritas unitmasuk → DMS ──
    if 'no_polisi' not in master.columns:
        master['no_polisi'] = None
    if 'no_polisi_um' in master.columns:
        master['no_polisi'] = master['no_polisi_um'].fillna(master['no_polisi'])

    # ── Hitung batas_tcare, in_tcare, sisa_bulan_tcare ──
    tgl_do_dt = pd.to_datetime(master['tgl_do'], errors='coerce')
    # Nullify dummy dates dari TAM (1900-xx-xx dan 3000-xx-xx)
    tgl_do_dt = tgl_do_dt.where(~tgl_do_dt.dt.year.isin([1900, 3000]))
    master['tgl_do'] = tgl_do_dt.dt.strftime('%Y-%m-%d')
    batas      = (tgl_do_dt + pd.DateOffset(months=36)
                 ).dt.to_period('M').dt.to_timestamp('M')
    today      = pd.Timestamp.today()

    master['batas_tcare']      = batas.dt.strftime('%Y-%m-%d')
    master['in_tcare']         = (batas >= today).astype(int)
    master['sisa_bulan_tcare'] = (
        (batas - today) / pd.Timedelta(days=30)
    ).round(1).clip(lower=0)

    # ── Parse kecamatan & kabupaten ──
    if 'alamat_stnk' in master.columns:
        parsed = master['alamat_stnk'].apply(
            lambda x: pd.Series(parse_alamat(x), index=['kecamatan', 'kabupaten'])
        )
        master['kecamatan'] = parsed['kecamatan']
        master['kabupaten'] = parsed['kabupaten']
    else:
        if 'kecamatan' not in master.columns:
            master['kecamatan'] = None
        if 'kabupaten' not in master.columns:
            master['kabupaten'] = None

    # ── Kolom final ──
    for col in ['no_polisi', 'telp_gsm', 'nama_sales']:
        if col not in master.columns:
            master[col] = None

    final_cols = [
        'no_rangka', 'customer', 'tgl_do', 'no_polisi', 'telp_gsm',
        'nama_sales', 'batas_tcare', 'in_tcare', 'sisa_bulan_tcare',
        'kecamatan', 'kabupaten', 'dealer_penjual', 'dealer_kategori',
    ]
    master = master[final_cols].copy()
    master = master.drop_duplicates(subset=['no_rangka'], keep='first')

    own_n    = (master['dealer_kategori'] == 'OWN').sum()
    berkah_n = (master['dealer_kategori'] == 'BERKAH').sum()
    print(f"  → rs: {len(master):,} unit ({own_n:,} OWN, {berkah_n:,} BERKAH)")
    return master


# ════════════════════════════════════════
# FUNGSI RINGKAS UNTUK ETL LAMA (backward compat)
# ════════════════════════════════════════

def load_rs(folder: str) -> pd.DataFrame:
    """Untuk enrich unitmasuk — subset kolom saja."""
    df = load_rs_tegal(folder)
    if len(df) == 0:
        return pd.DataFrame(columns=['no_rangka', 'tgl_do', 'batas_tcare', 'nama_sales'])

    tgl_do_dt  = pd.to_datetime(df['tgl_do'], errors='coerce')
    batas      = (tgl_do_dt + pd.DateOffset(months=36)
                 ).dt.to_period('M').dt.to_timestamp('M')
    df['batas_tcare'] = batas.dt.strftime('%Y-%m-%d')
    return df[['no_rangka', 'tgl_do', 'batas_tcare', 'nama_sales']]


# ════════════════════════════════════════
# RUN
# ════════════════════════════════════════

def run(paths: dict = None, map_cache=None):
    folder = (paths or {}).get('rs', PATHS['rs'])
    print("\n  Load RS (master unit)...")
    df = build_rs(folder, df_map_cache=map_cache)

    conn = sqlite3.connect(DB_PATH)
    df.to_sql('rs', conn, if_exists='replace', index=False)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_rs_rangka ON rs(no_rangka)')

    # Buat VIEW unit_tcare kalau belum ada
    conn.execute("DROP VIEW IF EXISTS unit_tcare")
    conn.execute("""
        CREATE VIEW unit_tcare AS
        SELECT r.*, t.tcare_type, t.sisa_service, t.sisa_detail,
               t.next_service, t.last_sbe_km, t.last_sbe_date,
               t.last_sbe_dealer, t.last_sbe_source, t.aktif_kategori,
               t.sa_terakhir, t.tgl_sa_terakhir,
               t.flag_pending_sbe, t.flag_sa,
               t.flag_tgl_kunjungan, t.flag_wo_type,
               t.next_sbe_expected, t.last_updated
        FROM rs r
        LEFT JOIN tcare_unit t ON r.no_rangka = t.no_rangka
    """)
    conn.commit()
    conn.close()
    print(f"  ✅ rs saved: {len(df):,} unit")


if __name__ == '__main__':
    run()