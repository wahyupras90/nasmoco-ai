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
    """Vectorized extract km dari kolom pekerjaan SBE/SBI."""
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
    """Ambil SBE/SBI tertinggi per no_rangka dari unitmasuk (vectorized).
    Include SBI (1K) agar last_sbe_km bisa capture servis pertama.
    Pakai kolom tanggal (WO masuk), bukan tgl_invoice — WO bisa belum
    ter-invoice tapi unit sudah datang servis.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT no_rangka, pekerjaan, tanggal, sa
            FROM unitmasuk
            WHERE kelompok IN ('SBE', 'SBI')
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
    result = df.loc[idx, ['no_rangka', 'km', 'tanggal', 'sa']].copy()
    return result.rename(columns={
        'km':      'last_sbe_km_um',
        'tanggal': 'last_sbe_date_um',
        'sa':      'last_sbe_dealer_um',
    })


def load_sbe_from_mapping_cust(df_map=None) -> pd.DataFrame:
    """
    Ambil last SBE/SBI dari Mapping Cust (2 file terbaru, vectorized via melt).
    Include SBI 1K (kolom 'SBI 1K' = tanggal, 'SBI 1K.1' = dealer hasil
    auto-rename pandas karena nama kolom asli duplikat di source CSV).
    df_map: opsional raw DataFrame Mapping Cust (kolom No. CHASSIS harus ada).
            Jika None, baca dari file.
    """
    if df_map is not None and 'No. CHASSIS' in df_map.columns:
        # Gunakan raw cache — clean dan dedup
        df = df_map.copy()
        df['no_rangka'] = (df['No. CHASSIS']
                           .astype(str).str.strip().str.lstrip('.').str.strip())
        df = df[df['no_rangka'].str.len() > 5]
        df = df[df['no_rangka'] != 'nan']
        df = df.drop_duplicates(subset=['no_rangka'], keep='first')
    else:
        # Baca dari file
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
        df['no_rangka'] = (df['No. CHASSIS']
                           .astype(str).str.strip().str.lstrip('.').str.strip())
        df = df[df['no_rangka'].str.len() > 5]
        df = df[df['no_rangka'] != 'nan']
        df = df.drop_duplicates(subset=['no_rangka'], keep='first')

    # Kolom SBE 10K-60K yang valid (kelipatan 10K saja, sesuai TCARE)
    valid_km_cols = [(f'SBE {k}K', k * 1000)
                     for k in range(5, 205, 5)
                     if f'SBE {k}K' in df.columns and k * 1000 in KM_TO_SERVICE]

    # Tambah SBI 1K — kolom dealer-nya adalah 'SBI 1K.1' (auto-rename pandas
    # karena CSV sumber punya 2 kolom dengan nama sama persis "SBI 1K")
    has_sbi = 'SBI 1K' in df.columns

    if not valid_km_cols and not has_sbi:
        return pd.DataFrame({'no_rangka': df['no_rangka'],
                             'last_sbe_km_map': None,
                             'last_sbe_date_map': None,
                             'last_sbe_dealer_map': None,
                             'aktif_kategori': df.get('Kategori', None)})

    # Melt ke long format — vectorized pd.to_datetime, tanpa apply per baris
    sbe_parts = []
    for col, km in valid_km_cols:
        dealer_col = f'Dealer {col}'
        mask = df[col].notna() & (df[col].astype(str).str.strip() != '')
        sub = df.loc[mask, ['no_rangka', col]].copy()
        if len(sub) == 0:
            continue
        sub['km'] = km
        sub['date'] = pd.to_datetime(
            sub[col], dayfirst=True, errors='coerce'
        ).dt.strftime('%Y-%m-%d')
        sub['dealer'] = df.loc[sub.index, dealer_col] if dealer_col in df.columns else None
        sbe_parts.append(sub[['no_rangka', 'km', 'date', 'dealer']])

    # SBI 1K — pakai kolom 'SBI 1K' (tanggal) + 'SBI 1K.1' (dealer)
    if has_sbi:
        mask = df['SBI 1K'].notna() & (df['SBI 1K'].astype(str).str.strip() != '')
        sub = df.loc[mask, ['no_rangka', 'SBI 1K']].copy()
        if len(sub) > 0:
            sub['km'] = 1000
            sub['date'] = pd.to_datetime(
                sub['SBI 1K'], dayfirst=True, errors='coerce'
            ).dt.strftime('%Y-%m-%d')
            sub['dealer'] = (df.loc[sub.index, 'SBI 1K.1']
                             if 'SBI 1K.1' in df.columns else None)
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


def load_sbe_from_tcare_nasional() -> pd.DataFrame:
    """
    Ambil last SBE/SBI dari T-CARE Nasional (2 file terbaru).
    Kolom: 'Bulan 1st Service' s.d. 'Bulan 7th Service' (tanggal lengkap,
    nama kolom menyesatkan tapi isinya YYYY-MM-DD), beserta
    'Dealer Nth Service' untuk dealer.
    1st Service = SBI (1K), 2nd-7th = SBE 10K-60K.
    """
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

    # Mapping ordinal service → km
    ORDINAL_TO_KM = {
        '1st': 1000, '2nd': 10000, '3rd': 20000, '4th': 30000,
        '5th': 40000, '6th': 50000, '7th': 60000,
    }

    sbe_parts = []
    for ordinal, km in ORDINAL_TO_KM.items():
        col_bulan  = f'Bulan {ordinal} Service'
        col_dealer = f'Dealer {ordinal} Service'
        if col_bulan not in df.columns:
            continue
        mask = df[col_bulan].notna()
        sub = df.loc[mask, ['no_rangka', col_bulan]].copy()
        if len(sub) == 0:
            continue
        sub['km'] = km
        sub['date'] = pd.to_datetime(
            sub[col_bulan], dayfirst=True, errors='coerce'
        ).dt.strftime('%Y-%m-%d')
        sub['dealer'] = df.loc[sub.index, col_dealer] if col_dealer in df.columns else None
        sbe_parts.append(sub[['no_rangka', 'km', 'date', 'dealer']])

    if not sbe_parts:
        return pd.DataFrame()

    sbe_all = pd.concat(sbe_parts, ignore_index=True)
    idx_max = sbe_all.groupby('no_rangka')['km'].idxmax()
    result = sbe_all.loc[idx_max].rename(columns={
        'km':     'last_sbe_km_tc',
        'date':   'last_sbe_date_tc',
        'dealer': 'last_sbe_dealer_tc',
    })
    print(f"  → TCARE Nasional SBE: {len(result):,} unit")
    return result[['no_rangka', 'last_sbe_km_tc', 'last_sbe_date_tc', 'last_sbe_dealer_tc']]


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
    """Ambil SA terakhir per no_rangka dari unitmasuk.
    Pakai tanggal WO (kolom tanggal), bukan tgl_invoice.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT u.no_rangka, u.sa AS sa_terakhir,
                   u.tanggal AS tgl_sa_terakhir
            FROM unitmasuk u
            INNER JOIN (
                SELECT no_rangka, MAX(tanggal) AS max_tgl
                FROM unitmasuk
                GROUP BY no_rangka
            ) m ON u.no_rangka = m.no_rangka
              AND u.tanggal = m.max_tgl
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
    # Prioritas 1: dari T-CARE Nasional
    if pd.notna(row.get('tcare_type_tc')):
        return row['tcare_type_tc']
    # Prioritas 2: dari model (aktif maupun expired)
    model = str(row.get('model', '') or '').upper()
    if model and model != 'NAN':
        if 'AGYA'   in model: return 'T-CARE LITE+'
        if 'CALYA'  in model: return 'T-CARE LITE'
        if 'RANGGA' in model: return 'RANGGA'
        return 'T-CARE'
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
    merged['tgl_invoice']   = pd.to_datetime(merged['tgl_invoice'],  errors='coerce')
    merged['last_sbe_date'] = pd.to_datetime(merged['last_sbe_date'], errors='coerce')
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

def run(paths: dict = None, map_cache=None):
    print("\n  Load TCARE Unit (vectorized)...")
    t0 = datetime.now()
    import time

    t = time.time()
    df_sbe_um  = load_sbe_from_unitmasuk()
    print(f"    sbe_um        : {time.time()-t:.1f}s"); t = time.time()

    df_sbe_map = load_sbe_from_mapping_cust(df_map=map_cache)
    print(f"    sbe_map       : {time.time()-t:.1f}s"); t = time.time()

    df_sbe_tc  = load_sbe_from_tcare_nasional()
    print(f"    sbe_tc        : {time.time()-t:.1f}s"); t = time.time()

    df_tc_type = load_tcare_type_from_nasional()
    print(f"    tc_type       : {time.time()-t:.1f}s"); t = time.time()

    df_sa      = load_sa_terakhir()
    print(f"    sa_terakhir   : {time.time()-t:.1f}s"); t = time.time()

    df_wo      = load_wo_non_sbe()
    print(f"    wo_non_sbe    : {time.time()-t:.1f}s"); t = time.time()

    conn = sqlite3.connect(DB_PATH)
    df_rs = pd.read_sql("SELECT no_rangka, dealer_kategori, batas_tcare FROM rs", conn)
    try:
        df_model = pd.read_sql("SELECT no_rangka, model FROM rs", conn)
    except Exception:
        df_model = pd.DataFrame()
    conn.close()
    print(f"    load_rs       : {time.time()-t:.1f}s"); t = time.time()

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
    if len(df_sbe_tc) > 0:
        master = master.merge(df_sbe_tc, on='no_rangka', how='left')

    # Pilih km tertinggi dari 3 sumber (vectorized, ignore NaN)
    km_um  = pd.to_numeric(master.get('last_sbe_km_um',  pd.Series(dtype=float)), errors='coerce')
    km_map = pd.to_numeric(master.get('last_sbe_km_map', pd.Series(dtype=float)), errors='coerce')
    km_tc  = pd.to_numeric(master.get('last_sbe_km_tc',  pd.Series(dtype=float)), errors='coerce')

    km_stack = pd.concat([km_um.rename('unitmasuk'),
                          km_map.rename('mapping_cust'),
                          km_tc.rename('tcare_nasional')], axis=1)
    master['last_sbe_km'] = km_stack.max(axis=1, skipna=True)

    # idxmax gagal kalau ada baris all-NaN, jadi isi NaN dengan -1 dulu
    # (tidak akan terpilih sebagai max kecuali semua kolom NaN)
    km_stack_filled = km_stack.fillna(-1)
    has_any = km_stack.notna().any(axis=1)
    master['last_sbe_source'] = None
    master.loc[has_any, 'last_sbe_source'] = (
        km_stack_filled.loc[has_any].idxmax(axis=1)
    )

    # Ambil date & dealer sesuai source terpilih
    # Normalisasi tanggal ke string YYYY-MM-DD dulu (unitmasuk balik dari SQL
    # sebagai Timestamp, mapping_cust/tcare_nasional sudah string) agar tidak
    # ada konflik tipe saat di-assign campuran ke kolom yang sama.
    date_um  = pd.to_datetime(master.get('last_sbe_date_um'),  errors='coerce').dt.strftime('%Y-%m-%d') \
               if 'last_sbe_date_um' in master.columns else pd.Series(index=master.index, dtype=object)
    date_map_col = master.get('last_sbe_date_map', pd.Series(index=master.index, dtype=object))
    date_tc  = master.get('last_sbe_date_tc',  pd.Series(index=master.index, dtype=object))

    date_map = {
        'unitmasuk':      date_um,
        'mapping_cust':   date_map_col,
        'tcare_nasional': date_tc,
    }
    dealer_map = {
        'unitmasuk':      master.get('last_sbe_dealer_um'),
        'mapping_cust':   master.get('last_sbe_dealer_map'),
        'tcare_nasional': master.get('last_sbe_dealer_tc'),
    }
    master['last_sbe_date']   = None
    master['last_sbe_dealer'] = None

    for src in ['unitmasuk', 'mapping_cust', 'tcare_nasional']:
        mask = (master['last_sbe_source'] == src).values
        if date_map[src] is not None:
            master.loc[mask, 'last_sbe_date'] = date_map[src].values[mask]
        if dealer_map[src] is not None:
            master.loc[mask, 'last_sbe_dealer'] = dealer_map[src].values[mask]

    # Hitung sisa vectorized
    sisa_df = calc_sisa_vectorized(master['last_sbe_km'])
    master['sisa_service']      = sisa_df['sisa_service']
    master['sisa_detail']       = sisa_df['sisa_detail']
    master['next_sbe_expected'] = sisa_df['next_sbe_expected']

    # Override dari T-CARE Nasional kalau source tcare_nasional & sisa_tc ada
    if 'sisa_tc' in master.columns:
        mask = (master['last_sbe_source'] == 'tcare_nasional') & master['sisa_tc'].notna()
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

    # ── Build tcare_schedule + tcare_monthly ──
    rebuild_tcare_monthly()


# ════════════════════════════════════════
# REBUILD TCARE SCHEDULE + MONTHLY
# ════════════════════════════════════════

def rebuild_tcare_monthly():
    """
    Rebuild tcare_schedule dan tcare_monthly dari DB.
    Dipanggil dari run() (monthly) dan run_tcare_unit_daily() (daily).
    Tidak baca file eksternal — hanya dari rs dan unitmasuk.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        df_schedule = build_tcare_schedule(conn)
        if len(df_schedule) > 0:
            df_schedule.to_sql('tcare_schedule', conn,
                               if_exists='replace', index=False)
            conn.execute('CREATE INDEX IF NOT EXISTS idx_tcs_rangka '
                         'ON tcare_schedule(no_rangka)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_tcs_bulan '
                         'ON tcare_schedule(bulan_jadwal)')
            conn.commit()

            df_monthly = build_tcare_monthly(df_schedule)
            if len(df_monthly) > 0:
                df_monthly.to_sql('tcare_monthly', conn,
                                  if_exists='replace', index=False)
                conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════
# TCARE SCHEDULE — detail per unit per kunjungan
# ════════════════════════════════════════

KUNJUNGAN_BULAN = {1: 6, 2: 12, 3: 18, 4: 24, 5: 30, 6: 36}  # kunjungan ke-N → +N bulan
KUNJUNGAN_LABEL = {1: '10K', 2: '20K', 3: '30K', 4: '40K', 5: '50K', 6: '60K'}


def build_tcare_schedule(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Generate jadwal TCARE per unit per kunjungan (6 kunjungan × unit OWN+BERKAH).
    Potensi = OWN only. BERKAH masuk tapi tidak dihitung sebagai potensi.
    Unit tanpa tgl_do → exclude.
    Unit expired (>36 bulan dari tgl_do) → expired=1.
    """
    df_rs = pd.read_sql("""
        SELECT no_rangka, dealer_kategori, tgl_do, customer
        FROM rs
        WHERE tgl_do IS NOT NULL
          AND tgl_do NOT IN ('1900-01-01','3000-01-01')
          AND tgl_do != ''
    """, conn)

    if len(df_rs) == 0:
        return pd.DataFrame()

    df_rs['tgl_do'] = pd.to_datetime(df_rs['tgl_do'], errors='coerce')
    df_rs = df_rs.dropna(subset=['tgl_do'])

    today = pd.Timestamp.today().normalize()

    # Ambil realisasi TCARE dari unitmasuk (include pekerjaan untuk mapping km → kunjungan)
    df_real = pd.read_sql("""
        SELECT no_rangka, no_wo, sa, tanggal, tgl_invoice, pekerjaan
        FROM unitmasuk
        WHERE tcare = 'TCARE'
          AND no_rangka IS NOT NULL
    """, conn)
    df_real['tanggal']     = pd.to_datetime(df_real['tanggal'], errors='coerce')
    df_real['tgl_invoice'] = pd.to_datetime(df_real['tgl_invoice'], errors='coerce')
    df_real['bulan_real']  = df_real['tanggal'].dt.strftime('%Y-%m')

    # Generate 6 kunjungan per unit (vectorized via concat)
    rows = []
    for kunjungan, bulan_tambah in KUNJUNGAN_BULAN.items():
        sub = df_rs.copy()
        sub['kunjungan']    = kunjungan
        sub['pekerjaan']    = KUNJUNGAN_LABEL[kunjungan]
        sub['bulan_jadwal'] = (sub['tgl_do'] + pd.DateOffset(months=bulan_tambah)).dt.strftime('%Y-%m')
        sub['tgl_jadwal']   = sub['tgl_do'] + pd.DateOffset(months=bulan_tambah)
        sub['expired'] = (
            sub['tgl_jadwal'].dt.to_period('M') < pd.Timestamp.today().to_period('M')
        ).astype(int)
        rows.append(sub)

    schedule = pd.concat(rows, ignore_index=True)

    # Step 1: Hitung bulan offset aktual dari tgl_do untuk setiap realisasi
    df_rs_do = df_rs[['no_rangka', 'tgl_do']].copy()
    df_real2 = df_real.merge(df_rs_do, on='no_rangka', how='left')
    df_real2 = df_real2.dropna(subset=['tgl_do'])
    df_real2['bulan_offset'] = (
        (df_real2['tanggal'].dt.year  - df_real2['tgl_do'].dt.year) * 12 +
        (df_real2['tanggal'].dt.month - df_real2['tgl_do'].dt.month)
    )

    # Tentukan kunjungan ke berapa berdasarkan KM AKTUAL (dari pekerjaan)
    # Prioritas km aktual > bulan_offset untuk menghindari salah mapping
    KM_TO_KUNJUNGAN = {10000:1, 20000:2, 30000:3, 40000:4, 50000:5, 60000:6}

    df_real2['km_actual'] = extract_km_series(df_real2['pekerjaan'])

    def map_kunjungan_km(row):
        """Prioritas: km aktual → bulan_offset sebagai fallback."""
        km = row.get('km_actual')
        if pd.notna(km) and int(km) in KM_TO_KUNJUNGAN:
            return KM_TO_KUNJUNGAN[int(km)]
        offset = row.get('bulan_offset')
        if pd.isna(offset):
            return None
        for k, bln in KUNJUNGAN_BULAN.items():
            if offset <= bln:
                return k
        return 6

    df_real2['kunjungan_real'] = df_real2.apply(map_kunjungan_km, axis=1)

    def get_status(row):
        kunjungan = row.get('kunjungan_real')
        if kunjungan is None or pd.isna(row.get('bulan_offset')):
            return 'unknown'
        jadwal_bln = KUNJUNGAN_BULAN.get(kunjungan)
        if jadwal_bln is None:
            return 'unknown'
        diff = row['bulan_offset'] - jadwal_bln
        if diff == 0:   return 'punctual'
        elif diff < 0:  return 'early'
        else:           return 'late'

    df_real2['status_real'] = df_real2.apply(get_status, axis=1)

    df_real3 = df_real2[['no_rangka', 'kunjungan_real', 'no_wo', 'sa',
                          'bulan_real', 'status_real']].rename(columns={
        'kunjungan_real': 'kunjungan',
        'no_wo':          'no_wo_real',
        'sa':             'sa_realisasi',
        'bulan_real':     'bulan_realisasi',
        'status_real':    'status',
    })

    df_real3 = df_real3.sort_values('bulan_realisasi', ascending=False)
    df_real3 = df_real3.drop_duplicates(subset=['no_rangka', 'kunjungan'])

    schedule = schedule.merge(df_real3, on=['no_rangka', 'kunjungan'], how='left')

    schedule['status'] = schedule['status'].fillna('pending')

    final_cols = [
        'no_rangka', 'dealer_kategori', 'tgl_do', 'kunjungan', 'pekerjaan',
        'bulan_jadwal', 'bulan_realisasi', 'status',
        'no_wo_real', 'sa_realisasi', 'expired',
    ]
    schedule['tgl_do'] = schedule['tgl_do'].dt.strftime('%Y-%m-%d')
    schedule = schedule[final_cols].copy()

    print(f"  → tcare_schedule: {len(schedule):,} baris "
          f"({schedule['expired'].sum():,} expired)")
    return schedule


# ════════════════════════════════════════
# TCARE MONTHLY — rekap agregat per bulan per pekerjaan
# ════════════════════════════════════════

def build_tcare_monthly(df_schedule: pd.DataFrame) -> pd.DataFrame:
    """
    Rekap agregat per bulan per pekerjaan dari tcare_schedule.
    Potensi = OWN only (non-expired).
    Realisasi = OWN + BERKAH.
    Punctual/early/late = OWN only.
    """
    if len(df_schedule) == 0:
        return pd.DataFrame()

    pot = (df_schedule[
                (df_schedule['dealer_kategori'] == 'OWN') &
                (df_schedule['expired'] == 0)
           ]
           .groupby(['bulan_jadwal', 'pekerjaan'])
           .size()
           .reset_index(name='potensi'))

    real_all = df_schedule[df_schedule['bulan_realisasi'].notna()]

    real_total = (real_all
                  .groupby(['bulan_realisasi', 'pekerjaan'])
                  .size()
                  .reset_index(name='realisasi')
                  .rename(columns={'bulan_realisasi': 'bulan'}))

    real_own = (real_all[real_all['dealer_kategori'] == 'OWN']
                .groupby(['bulan_realisasi', 'pekerjaan'])
                .size()
                .reset_index(name='real_own')
                .rename(columns={'bulan_realisasi': 'bulan'}))

    real_berkah = (real_all[real_all['dealer_kategori'] == 'BERKAH']
                   .groupby(['bulan_realisasi', 'pekerjaan'])
                   .size()
                   .reset_index(name='real_berkah')
                   .rename(columns={'bulan_realisasi': 'bulan'}))

    own_real = real_all[real_all['dealer_kategori'] == 'OWN']

    punctual = (own_real[own_real['status'] == 'punctual']
                .groupby(['bulan_realisasi', 'pekerjaan'])
                .size().reset_index(name='punctual')
                .rename(columns={'bulan_realisasi': 'bulan'}))

    early = (own_real[own_real['status'] == 'early']
             .groupby(['bulan_realisasi', 'pekerjaan'])
             .size().reset_index(name='early')
             .rename(columns={'bulan_realisasi': 'bulan'}))

    late = (own_real[own_real['status'] == 'late']
            .groupby(['bulan_realisasi', 'pekerjaan'])
            .size().reset_index(name='late')
            .rename(columns={'bulan_realisasi': 'bulan'}))

    monthly = pot.rename(columns={'bulan_jadwal': 'bulan'})
    monthly = monthly.merge(real_total,  on=['bulan', 'pekerjaan'], how='left')
    monthly = monthly.merge(real_own,    on=['bulan', 'pekerjaan'], how='left')
    monthly = monthly.merge(real_berkah, on=['bulan', 'pekerjaan'], how='left')
    monthly = monthly.merge(punctual,    on=['bulan', 'pekerjaan'], how='left')
    monthly = monthly.merge(early,       on=['bulan', 'pekerjaan'], how='left')
    monthly = monthly.merge(late,        on=['bulan', 'pekerjaan'], how='left')

    for col in ['realisasi', 'real_own', 'real_berkah', 'punctual', 'early', 'late']:
        monthly[col] = monthly[col].fillna(0).astype(int)

    monthly['conversion'] = (
        monthly['realisasi'] * 100.0 /
        monthly['potensi'].replace(0, pd.NA)
    ).round(1)

    pekerjaan_order = ['10K', '20K', '30K', '40K', '50K', '60K']
    monthly['pekerjaan'] = pd.Categorical(
        monthly['pekerjaan'], categories=pekerjaan_order, ordered=True
    )
    monthly = monthly.sort_values(['bulan', 'pekerjaan']).reset_index(drop=True)

    print(f"  → tcare_monthly: {len(monthly):,} baris")
    return monthly


# ════════════════════════════════════════
# DAILY UPDATE TCARE UNIT
# ════════════════════════════════════════

def _get_tcare_type_from_model(model: str) -> str:
    """Tentukan tcare_type sementara dari nama model."""
    if not model:
        return 'T-CARE'
    m = str(model).upper()
    if 'AGYA'   in m: return 'T-CARE LITE+'
    if 'CALYA'  in m: return 'T-CARE LITE'
    if 'RANGGA' in m: return 'RANGGA'
    return 'T-CARE'


def run_tcare_unit_daily():
    """
    Update ringan tcare_unit berdasarkan WO baru sejak last_updated.
    - Update SBE/SBI: last_sbe_km, last_sbe_date, sisa_service, dll
    - Update SA terakhir dari WO terbaru
    - Insert unit baru (belum ada di tcare_unit) dengan data parsial dari rs
    - TIDAK rebuild tcare_schedule dan tcare_monthly (dipanggil terpisah)
    """
    t0 = datetime.now()
    print("\n  Daily update TCARE Unit...")

    conn = sqlite3.connect(DB_PATH)

    # 1. Ambil last_updated dari tcare_unit
    try:
        last_upd = conn.execute(
            "SELECT MIN(last_updated) FROM tcare_unit"
        ).fetchone()[0]
        if not last_upd:
            last_upd = '2020-01-01'
    except Exception:
        last_upd = '2020-01-01'
    print(f"    last_updated  : {last_upd}")

    # 2. Ambil WO baru sejak last_updated (termasuk SBI)
    try:
        df_new = pd.read_sql(f"""
            SELECT no_rangka, tanggal, tgl_invoice, sa,
                   kelompok, pekerjaan
            FROM unitmasuk
            WHERE kelompok IN ('SBE', 'SBI', 'GRP', 'LUB')
              AND no_rangka IS NOT NULL
              AND tanggal >= '{last_upd}'
        """, conn)
    except Exception as e:
        print(f"  ⚠ Gagal query unitmasuk: {e}")
        conn.close()
        return

    if len(df_new) == 0:
        print("    Tidak ada WO baru.")
        conn.close()
        return
    print(f"    WO baru       : {len(df_new):,} baris "
          f"({df_new['no_rangka'].nunique():,} unit)")

    # 3. Load tcare_unit saat ini
    df_tcu = pd.read_sql("SELECT * FROM tcare_unit", conn)
    existing_nr = set(df_tcu['no_rangka'].dropna())

    # 4. Load rs untuk unit baru
    df_rs = pd.read_sql("""
        SELECT no_rangka, dealer_kategori, batas_tcare, model,
               tgl_do, customer
        FROM rs
    """, conn)

    conn.close()

    # ── A. Update SBE (termasuk SBI/1K) ──
    df_sbe = df_new[df_new['kelompok'].isin(['SBE', 'SBI'])].copy()
    if len(df_sbe) > 0:
        df_sbe['km'] = extract_km_series(df_sbe['pekerjaan'])
        df_sbe = df_sbe.dropna(subset=['km'])
        df_sbe['km'] = df_sbe['km'].astype(int)

        idx = df_sbe.groupby('no_rangka')['km'].idxmax()
        sbe_best = df_sbe.loc[idx].set_index('no_rangka')

        df_tcu = df_tcu.set_index('no_rangka')
        for nr, row in sbe_best.iterrows():
            new_km = int(row['km'])
            if nr in df_tcu.index:
                cur_km = df_tcu.at[nr, 'last_sbe_km']
                cur_km = int(cur_km) if pd.notna(cur_km) else -1
                if new_km > cur_km:
                    df_tcu.at[nr, 'last_sbe_km']     = new_km
                    df_tcu.at[nr, 'last_sbe_date']   = row['tanggal']
                    df_tcu.at[nr, 'last_sbe_dealer']  = row['sa']
                    df_tcu.at[nr, 'last_sbe_source']  = 'unitmasuk'
        df_tcu = df_tcu.reset_index()

        sisa_df = calc_sisa_vectorized(df_tcu['last_sbe_km'])
        df_tcu['sisa_service']      = sisa_df['sisa_service']
        df_tcu['sisa_detail']       = sisa_df['sisa_detail']
        df_tcu['next_sbe_expected'] = sisa_df['next_sbe_expected']
        df_tcu['next_service']      = df_tcu['next_sbe_expected']

    # ── B. Update SA terakhir ──
    idx_sa = df_new.groupby('no_rangka')['tanggal'].idxmax()
    sa_latest = df_new.loc[idx_sa, ['no_rangka', 'sa', 'tanggal']].set_index('no_rangka')

    df_tcu = df_tcu.set_index('no_rangka')
    for nr, row in sa_latest.iterrows():
        if nr in df_tcu.index:
            cur_tgl = df_tcu.at[nr, 'tgl_sa_terakhir']
            if pd.isna(cur_tgl) or str(row['tanggal']) >= str(cur_tgl):
                df_tcu.at[nr, 'sa_terakhir']     = row['sa']
                df_tcu.at[nr, 'tgl_sa_terakhir'] = row['tanggal']
    df_tcu = df_tcu.reset_index()

    # ── C. Insert unit baru ──
    new_units = set(df_new['no_rangka'].dropna()) - existing_nr
    if new_units:
        print(f"    Unit baru     : {len(new_units):,}")
        df_rs_idx = df_rs.set_index('no_rangka')
        new_rows = []
        for nr in new_units:
            rs_row = df_rs_idx.loc[nr] if nr in df_rs_idx.index else None
            dealer_kat = rs_row['dealer_kategori'] if rs_row is not None else 'BERKAH'
            batas      = rs_row['batas_tcare']     if rs_row is not None else None
            model      = rs_row['model']            if rs_row is not None else None

            tcare_type = None
            if batas and pd.notna(batas):
                try:
                    if pd.to_datetime(batas) >= pd.Timestamp.today():
                        tcare_type = _get_tcare_type_from_model(model)
                except Exception:
                    pass

            unit_sbe = df_new[
                (df_new['no_rangka'] == nr) & (df_new['kelompok'].isin(['SBE', 'SBI']))
            ].copy()
            last_sbe_km = last_sbe_date = last_sbe_dealer = None
            if len(unit_sbe) > 0:
                unit_sbe['km'] = extract_km_series(unit_sbe['pekerjaan'])
                unit_sbe = unit_sbe.dropna(subset=['km'])
                if len(unit_sbe) > 0:
                    best = unit_sbe.loc[unit_sbe['km'].idxmax()]
                    last_sbe_km     = int(best['km'])
                    last_sbe_date   = best['tanggal']
                    last_sbe_dealer = best['sa']

            sisa_df = calc_sisa_vectorized(pd.Series([last_sbe_km]))

            unit_all = df_new[df_new['no_rangka'] == nr]
            idx_sa_new = unit_all['tanggal'].idxmax()
            sa_t   = unit_all.at[idx_sa_new, 'sa']
            tgl_sa = unit_all.at[idx_sa_new, 'tanggal']

            new_rows.append({
                'no_rangka':        nr,
                'dealer_kategori':  dealer_kat,
                'tcare_type':       tcare_type,
                'sisa_service':     sisa_df.iloc[0]['sisa_service'],
                'sisa_detail':      sisa_df.iloc[0]['sisa_detail'],
                'next_service':     sisa_df.iloc[0]['next_sbe_expected'],
                'last_sbe_km':      last_sbe_km,
                'last_sbe_date':    last_sbe_date,
                'last_sbe_dealer':  last_sbe_dealer,
                'last_sbe_source':  'unitmasuk' if last_sbe_km else None,
                'aktif_kategori':   None,
                'sa_terakhir':      sa_t,
                'tgl_sa_terakhir':  tgl_sa,
                'flag_pending_sbe': False,
                'flag_sa':          None,
                'flag_tgl_kunjungan': None,
                'flag_wo_type':     None,
                'flag_next_sbe':    None,
                'next_sbe_expected': sisa_df.iloc[0]['next_sbe_expected'],
                'last_updated':     datetime.now().strftime('%Y-%m-%d'),
            })

        if new_rows:
            df_tcu = pd.concat(
                [df_tcu, pd.DataFrame(new_rows)],
                ignore_index=True
            )

    # ── D. Update flag pending SBE ──
    df_wo = df_new[df_new['kelompok'].isin(['GRP', 'LUB'])].copy()
    if len(df_wo) > 0:
        df_wo = df_wo.drop(columns=['tgl_invoice'], errors='ignore')
        df_wo = df_wo.rename(columns={'tanggal': 'tgl_invoice'})
        df_tcu = df_tcu.merge(
            df_rs[['no_rangka', 'batas_tcare']].rename(
                columns={'batas_tcare': '_batas'}),
            on='no_rangka', how='left'
        )
        if 'batas_tcare' not in df_tcu.columns:
            df_tcu['batas_tcare'] = df_tcu['_batas']
        else:
            df_tcu['batas_tcare'] = df_tcu['batas_tcare'].fillna(df_tcu['_batas'])
        df_tcu = df_tcu.drop(columns=['_batas'], errors='ignore')
        df_tcu = detect_pending_sbe_vectorized(df_tcu, df_wo)

    # ── E. Update last_updated ──
    df_tcu['last_updated'] = datetime.now().strftime('%Y-%m-%d')

    # ── F. Simpan ke DB ──
    conn = sqlite3.connect(DB_PATH)
    df_tcu.to_sql('tcare_unit', conn, if_exists='replace', index=False)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tcu_rangka ON tcare_unit(no_rangka)')
    conn.commit()
    conn.close()

    # ── G. Rebuild tcare_schedule + tcare_monthly ──
    rebuild_tcare_monthly()

    elapsed = (datetime.now() - t0).total_seconds()
    flag_n  = df_tcu['flag_pending_sbe'].sum()
    print(f"  ✅ tcare_unit daily: {len(df_tcu):,} unit "
          f"({int(flag_n)} pending SBE) — {elapsed:.1f} detik")


if __name__ == '__main__':
    run()