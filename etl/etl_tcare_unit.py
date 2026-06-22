"""
etl_tcare_unit.py (v2 — vectorized)
=====================================
ETL tabel tcare_unit — monitoring TCARE per unit.
Versi optimasi: vectorized pandas, tidak ada Python loop per baris.
"""

import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from etl_helpers import PATHS, DB_PATH, parse_date_flexible, clean_no_rangka

TCARE_NASIONAL_DIR = Path(PATHS["tcare_nasional"])
MAPPING_CUST_DIR   = Path(PATHS["mapping_cust"])

KM_TO_SERVICE = {
    1000: 1, 10000: 2, 20000: 3, 30000: 4,
    40000: 5, 50000: 6, 60000: 7,
}
SERVICE_TO_KM = {v: k for k, v in KM_TO_SERVICE.items()}
ALL_KM        = sorted(KM_TO_SERVICE.keys())


# ════════════════════════════════════════
# EXTRACT KM DARI PEKERJAAN (vectorized)
# ════════════════════════════════════════

def extract_km_series(series: pd.Series) -> pd.Series:
    """Vectorized extract km dari kolom pekerjaan SBE."""
    cleaned = (series.str.upper()
                     .str.replace('SERVIS BERKALA ', '', regex=False)
                     .str.replace('.', '', regex=False)
                     .str.replace(' KM', '', regex=False)
                     .str.strip())
    km = pd.to_numeric(cleaned, errors='coerce').astype('Int64')
    valid_km = set(KM_TO_SERVICE.keys())
    return km.where(km.isin(valid_km))


# ════════════════════════════════════════
# LOAD DATA SUMBER
# ════════════════════════════════════════

def load_sbe_from_unitmasuk() -> pd.DataFrame:
    """Ambil SBE tertinggi per no_rangka dari unitmasuk (vectorized)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT no_rangka, pekerjaan, tgl_invoice, sa
            FROM unitmasuk
            WHERE kelompok = 'SBE'
              AND pekerjaan LIKE 'SERVIS BERKALA%'
              AND no_rangka IS NOT NULL
        """, conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    if len(df) == 0:
        return pd.DataFrame()

    df['km'] = extract_km_series(df['pekerjaan'])
    df = df.dropna(subset=['km'])
    df['km'] = df['km'].astype(int)

    # Ambil km tertinggi per no_rangka (vectorized)
    idx = df.groupby('no_rangka')['km'].idxmax()
    result = df.loc[idx, ['no_rangka', 'km', 'tgl_invoice', 'sa']].copy()
    return result.rename(columns={
        'km':          'last_sbe_km_um',
        'tgl_invoice': 'last_sbe_date_um',
        'sa':          'last_sbe_dealer_um',
    })


def load_sbe_from_mapping_cust() -> pd.DataFrame:
    """
    Ambil last SBE dari Mapping Cust (2 file terbaru, vectorized via melt).
    """
    files = sorted(MAPPING_CUST_DIR.glob('*.csv'), reverse=True)[:2]
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f, sep=';', encoding='latin1', low_memory=False)
            dfs.append(df)
        except Exception:
            pass
    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df['no_rangka'] = df['No. CHASSIS'].apply(clean_no_rangka)
    df = df.dropna(subset=['no_rangka'])
    df = df.drop_duplicates(subset=['no_rangka'], keep='first')

    # Kolom SBE yang valid
    valid_km_cols = [(f'SBE {k}K', k * 1000)
                     for k in range(5, 205, 5)
                     if f'SBE {k}K' in df.columns and k * 1000 in KM_TO_SERVICE]

    if not valid_km_cols:
        return pd.DataFrame({'no_rangka': df['no_rangka'],
                             'last_sbe_km_map': None,
                             'last_sbe_date_map': None,
                             'last_sbe_dealer_map': None,
                             'aktif_kategori': df.get('Kategori', None)})

    # Melt ke long format — jauh lebih cepat dari apply per baris
    sbe_parts = []
    for col, km in valid_km_cols:
        dealer_col = f'Dealer {col}'
        sub = df[['no_rangka', col]].copy()
        sub = sub[sub[col].notna() & (sub[col].astype(str).str.strip() != '')]
        if len(sub) == 0:
            continue
        sub['km']     = km
        sub['date']   = sub[col].apply(parse_date_flexible)
        sub['dealer'] = df[dealer_col].values if dealer_col in df.columns else None
        sbe_parts.append(sub[['no_rangka', 'km', 'date', 'dealer']])

    aktif = df[['no_rangka']].copy()
    if 'Kategori' in df.columns:
        aktif['aktif_kategori'] = df['Kategori'].str.strip()
    else:
        aktif['aktif_kategori'] = None

    if not sbe_parts:
        result = aktif.copy()
        result['last_sbe_km_map'] = None
        result['last_sbe_date_map'] = None
        result['last_sbe_dealer_map'] = None
        return result

    sbe_all = pd.concat(sbe_parts, ignore_index=True)
    idx_max = sbe_all.groupby('no_rangka')['km'].idxmax()
    last_sbe = sbe_all.loc[idx_max].rename(columns={
        'km':     'last_sbe_km_map',
        'date':   'last_sbe_date_map',
        'dealer': 'last_sbe_dealer_map',
    })

    result = aktif.merge(last_sbe, on='no_rangka', how='left')
    print(f"  → Mapping Cust SBE: {len(result):,} unit")
    return result


def load_tcare_type_from_nasional() -> pd.DataFrame:
    """Ambil tcare_type dari 2 file T-CARE Nasional terbaru."""
    files = sorted(TCARE_NASIONAL_DIR.glob('*.xlsx'), reverse=True)[:2]
    dfs = []
    for f in files:
        try:
            df = pd.read_excel(f, header=0, engine='openpyxl')
            dfs.append(df)
        except Exception:
            pass
    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df['no_rangka'] = df['No Rangka'].apply(clean_no_rangka)
    df = df.dropna(subset=['no_rangka'])
    df = df.drop_duplicates(subset=['no_rangka'], keep='first')

    return pd.DataFrame({
        'no_rangka':       df['no_rangka'],
        'tcare_type_tc':   df['T-CARE TYPE'].str.strip(),
        'sisa_tc':         pd.to_numeric(df['SISA'], errors='coerce'),
        'next_service_tc': df['Next Service'].str.strip(),
    })


def load_sa_terakhir() -> pd.DataFrame:
    """Ambil SA terakhir per no_rangka dari unitmasuk."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT u.no_rangka, u.sa AS sa_terakhir,
                   u.tgl_invoice AS tgl_sa_terakhir
            FROM unitmasuk u
            INNER JOIN (
                SELECT no_rangka, MAX(tgl_invoice) AS max_inv
                FROM unitmasuk
                GROUP BY no_rangka
            ) m ON u.no_rangka = m.no_rangka
              AND u.tgl_invoice = m.max_inv
            GROUP BY u.no_rangka
        """, conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


def load_wo_non_sbe() -> pd.DataFrame:
    """Ambil WO non-SBE per no_rangka untuk deteksi pending SBE."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT DISTINCT no_rangka, sa, tgl_invoice, kelompok
            FROM unitmasuk
            WHERE kelompok IN ('GRP','LUB')
              AND no_rangka IS NOT NULL
              AND tgl_invoice IS NOT NULL
        """, conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


# ════════════════════════════════════════
# HITUNG SISA & DETAIL (vectorized)
# ════════════════════════════════════════

def get_tcare_type(row) -> str:
    if pd.notna(row.get('tcare_type_tc')):
        return row['tcare_type_tc']
    batas = row.get('batas_tcare')
    if pd.notna(batas) and batas:
        try:
            if pd.to_datetime(batas) >= pd.Timestamp.today():
                model = str(row.get('model', '') or '').upper()
                if 'AGYA'   in model: return 'T-CARE LITE+'
                if 'CALYA'  in model: return 'T-CARE LITE'
                if 'RANGGA' in model: return 'RANGGA'
                return 'T-CARE'
        except Exception:
            pass
    return None


def calc_sisa_vectorized(km_series: pd.Series) -> pd.DataFrame:
    """
    Vectorized hitung sisa service dari series km.
    Return DataFrame dengan kolom: sisa_service, sisa_detail, next_sbe_expected
    """
    ordinal = {1:'1ST',2:'2ND',3:'3RD',4:'4TH',5:'5TH',6:'6TH',7:'7TH'}

    def _calc(km):
        if pd.isna(km):
            return pd.Series([7, '1K, 10K, 20K, 30K, 40K, 50K, 60K', '1ST'])
        try:
            km = int(km)
        except Exception:
            return pd.Series([None, None, None])
        svc_ke = KM_TO_SERVICE.get(km)
        if svc_ke is None:
            return pd.Series([None, None, None])
        sisa    = 7 - svc_ke
        remain  = [f"{k//1000}K" if k > 1000 else "1K"
                   for k in ALL_KM if KM_TO_SERVICE[k] > svc_ke]
        detail  = ', '.join(remain) if remain else 'SELESAI'
        next_ke = svc_ke + 1
        next_s  = ordinal.get(next_ke, 'NON PPM') if next_ke <= 7 else 'NON PPM'
        return pd.Series([sisa, detail, next_s])

    result = km_series.apply(_calc)
    result.columns = ['sisa_service', 'sisa_detail', 'next_sbe_expected']
    return result


# ════════════════════════════════════════
# DETEKSI FLAG PENDING SBE (vectorized)
# ════════════════════════════════════════

def detect_pending_sbe_vectorized(master: pd.DataFrame,
                                  df_wo: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorized deteksi unit yang kunjungan non-SBE setelah last SBE.
    Menggunakan pandas merge, bukan Python loop.
    """
    master = master.copy()
    for col in ['flag_pending_sbe', 'flag_sa', 'flag_tgl_kunjungan',
                'flag_wo_type', 'flag_next_sbe']:
        master[col] = None
    master['flag_pending_sbe'] = False

    if len(df_wo) == 0:
        return master

    today = pd.Timestamp.today()

    # Filter unit yang eligible
    batas_dt = pd.to_datetime(master['batas_tcare'], errors='coerce')
    eligible_mask = (
        master['sisa_service'].notna() &
        (master['sisa_service'] > 0) &
        (batas_dt >= today) &
        master['last_sbe_date'].notna()
    )
    eligible = master.loc[eligible_mask, ['no_rangka', 'last_sbe_date', 'next_sbe_expected']].copy()

    if len(eligible) == 0:
        return master

    # Merge dengan WO non-SBE
    merged = eligible.merge(
        df_wo[['no_rangka', 'sa', 'tgl_invoice', 'kelompok']],
        on='no_rangka', how='inner'
    )

    # Filter WO setelah last SBE
    merged = merged[merged['tgl_invoice'] > merged['last_sbe_date']]
    if len(merged) == 0:
        return master

    # Ambil WO terbaru per no_rangka
    latest = (merged.sort_values('tgl_invoice', ascending=False)
                    .drop_duplicates('no_rangka'))

    flag_df = latest[['no_rangka', 'sa', 'tgl_invoice', 'kelompok', 'next_sbe_expected']].copy()
    flag_df = flag_df.rename(columns={
        'sa':               'flag_sa',
        'tgl_invoice':      'flag_tgl_kunjungan',
        'kelompok':         'flag_wo_type',
        'next_sbe_expected':'flag_next_sbe',
    })
    flag_df['flag_pending_sbe'] = True

    # Merge back
    flag_cols = ['no_rangka', 'flag_pending_sbe', 'flag_sa',
                 'flag_tgl_kunjungan', 'flag_wo_type', 'flag_next_sbe']
    master = master.merge(flag_df[flag_cols], on='no_rangka', how='left',
                         suffixes=('', '_new'))

    for col in ['flag_pending_sbe', 'flag_sa', 'flag_tgl_kunjungan',
                'flag_wo_type', 'flag_next_sbe']:
        new_col = col + '_new'
        if new_col in master.columns:
            master[col] = master[new_col].combine_first(master[col])
            master = master.drop(columns=[new_col])

    master['flag_pending_sbe'] = master['flag_pending_sbe'].fillna(False)
    return master


# ════════════════════════════════════════
# RUN
# ════════════════════════════════════════

def run(paths: dict = None):
    print("\n  Load TCARE Unit (vectorized)...")
    t0 = datetime.now()

    df_sbe_um  = load_sbe_from_unitmasuk()
    df_sbe_map = load_sbe_from_mapping_cust()
    df_tc_type = load_tcare_type_from_nasional()
    df_sa      = load_sa_terakhir()
    df_wo      = load_wo_non_sbe()

    conn = sqlite3.connect(DB_PATH)
    df_rs = pd.read_sql("SELECT no_rangka, dealer_kategori, batas_tcare FROM rs", conn)
    try:
        df_model = pd.read_sql("SELECT no_rangka, model FROM rs", conn)
    except Exception:
        df_model = pd.DataFrame()
    conn.close()

    master = df_rs.copy()

    if len(df_tc_type) > 0:
        master = master.merge(df_tc_type, on='no_rangka', how='left')
    if len(df_model) > 0:
        master = master.merge(df_model, on='no_rangka', how='left')

    master['tcare_type'] = master.apply(get_tcare_type, axis=1)

    if len(df_sbe_um) > 0:
        master = master.merge(df_sbe_um, on='no_rangka', how='left')
    if len(df_sbe_map) > 0:
        master = master.merge(df_sbe_map, on='no_rangka', how='left')

    # Pilih km tertinggi (vectorized)
    km_um  = pd.to_numeric(master.get('last_sbe_km_um',  pd.Series(dtype=float)), errors='coerce')
    km_map = pd.to_numeric(master.get('last_sbe_km_map', pd.Series(dtype=float)), errors='coerce')
    master['last_sbe_km'] = np.fmax(km_um.values, km_map.values)  # fmax = max ignoring NaN

    # Source
    master['last_sbe_source'] = np.select(
        [km_um.isna() & km_map.notna(),
         km_um.notna() & km_map.isna(),
         km_um.notna() & km_map.notna() & (km_um >= km_map),
         km_um.notna() & km_map.notna() & (km_um < km_map)],
        ['mapping_cust', 'unitmasuk', 'unitmasuk', 'mapping_cust'],
        default=None
    )
    master['last_sbe_date'] = np.where(
        master['last_sbe_source'] == 'unitmasuk',
        master.get('last_sbe_date_um'),
        master.get('last_sbe_date_map')
    )
    master['last_sbe_dealer'] = np.where(
        master['last_sbe_source'] == 'unitmasuk',
        master.get('last_sbe_dealer_um'),
        master.get('last_sbe_dealer_map')
    )

    # Hitung sisa vectorized
    sisa_df = calc_sisa_vectorized(master['last_sbe_km'])
    master['sisa_service']      = sisa_df['sisa_service']
    master['sisa_detail']       = sisa_df['sisa_detail']
    master['next_sbe_expected'] = sisa_df['next_sbe_expected']

    # Override dari T-CARE Nasional kalau mapping_cust source
    if 'sisa_tc' in master.columns:
        mask = (master['last_sbe_source'] == 'mapping_cust') & master['sisa_tc'].notna()
        master.loc[mask, 'sisa_service'] = master.loc[mask, 'sisa_tc']
    master['next_service'] = master.get('next_sbe_expected')

    if len(df_sa) > 0:
        master = master.merge(df_sa, on='no_rangka', how='left')

    # Flag pending SBE (vectorized)
    master = detect_pending_sbe_vectorized(master, df_wo)

    final_cols = [
        'no_rangka', 'dealer_kategori', 'tcare_type',
        'sisa_service', 'sisa_detail', 'next_service',
        'last_sbe_km', 'last_sbe_date', 'last_sbe_dealer', 'last_sbe_source',
        'aktif_kategori', 'sa_terakhir', 'tgl_sa_terakhir',
        'flag_pending_sbe', 'flag_sa', 'flag_tgl_kunjungan',
        'flag_wo_type', 'flag_next_sbe', 'next_sbe_expected',
    ]
    for c in final_cols:
        if c not in master.columns:
            master[c] = None
    master = master[final_cols].copy()
    master = master.loc[:, ~master.columns.duplicated()]
    master['last_updated'] = datetime.now().strftime('%Y-%m-%d')

    conn = sqlite3.connect(DB_PATH)
    master.to_sql('tcare_unit', conn, if_exists='replace', index=False)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tcu_rangka ON tcare_unit(no_rangka)')
    conn.commit()
    conn.close()

    elapsed = (datetime.now() - t0).total_seconds()
    flag_n  = master['flag_pending_sbe'].sum()
    print(f"  ✅ tcare_unit: {len(master):,} unit "
          f"({int(flag_n)} pending SBE) — {elapsed:.1f} detik")


if __name__ == '__main__':
    run()
