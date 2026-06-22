"""
etl_tcare_unit.py
=================
ETL tabel tcare_unit — monitoring TCARE per unit.

Kolom:
  no_rangka, dealer_kategori, tcare_type,
  sisa_service, sisa_detail, next_service,
  last_sbe_km, last_sbe_date, last_sbe_dealer, last_sbe_source,
  aktif_kategori, sa_terakhir, tgl_sa_terakhir,
  flag_pending_sbe, flag_sa, flag_tgl_kunjungan,
  flag_wo_type, next_sbe_expected, last_updated
"""

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime
from etl_helpers import (
    PATHS, DB_PATH, parse_date_flexible, clean_no_rangka
)

TCARE_NASIONAL_DIR = Path(PATHS["tcare_nasional"])
MAPPING_CUST_DIR   = Path(PATHS["mapping_cust"])

# Mapping km → service ke (1-7)
KM_TO_SERVICE = {
    1000:  1,   # SBI
    10000: 2,
    20000: 3,
    30000: 4,
    40000: 5,
    50000: 6,
    60000: 7,
}
SERVICE_TO_KM = {v: k for k, v in KM_TO_SERVICE.items()}
ALL_KM = sorted(KM_TO_SERVICE.keys())


# ════════════════════════════════════════
# EXTRACT KM DARI PEKERJAAN
# ════════════════════════════════════════

def extract_km_from_pekerjaan(pekerjaan: str):
    """
    Extract km dari string pekerjaan SBE.
    'SERVIS BERKALA 50.000 KM' → 50000
    'SERVIS BERKALA 1.000 KM'  → 1000
    """
    if not pekerjaan or pd.isna(pekerjaan):
        return None
    import re
    p = str(pekerjaan).upper().strip()
    if 'SERVIS BERKALA' not in p:
        return None
    try:
        clean = p.replace('SERVIS BERKALA ', '').replace('.', '').replace(' KM', '').strip()
        km = int(clean)
        return km if km in KM_TO_SERVICE else None
    except Exception:
        return None


# ════════════════════════════════════════
# LOAD DATA SUMBER
# ════════════════════════════════════════

def load_sbe_from_unitmasuk() -> pd.DataFrame:
    """Ambil SBE tertinggi per no_rangka dari unitmasuk."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT no_rangka, pekerjaan, tgl_invoice, sa, kelompok
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

    df['km'] = df['pekerjaan'].apply(extract_km_from_pekerjaan)
    df = df.dropna(subset=['km'])
    df['km'] = df['km'].astype(int)

    # Ambil km tertinggi per no_rangka
    idx = df.groupby('no_rangka')['km'].idxmax()
    result = df.loc[idx, ['no_rangka', 'km', 'tgl_invoice', 'sa']].copy()
    result = result.rename(columns={
        'km':          'last_sbe_km_um',
        'tgl_invoice': 'last_sbe_date_um',
        'sa':          'last_sbe_dealer_um',
    })
    return result


def load_sbe_from_mapping_cust() -> pd.DataFrame:
    """Ambil last SBE per no_rangka dari Mapping Cust."""
    files = sorted(MAPPING_CUST_DIR.glob('*.csv'), reverse=True)
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

    # Cari km tertinggi dari kolom SBE xK
    km_cols = [(f'SBE {k}K', k * 1000) for k in range(5, 205, 5)
               if f'SBE {k}K' in df.columns and k * 1000 in KM_TO_SERVICE]

    def get_last_sbe_map(row):
        last_km, last_date, last_dealer = None, None, None
        for col, km in km_cols:
            val = row.get(col)
            if pd.notna(val) and str(val).strip():
                last_km     = km
                last_date   = parse_date_flexible(val)
                dealer_col  = f'Dealer {col}'
                last_dealer = str(row.get(dealer_col, '')).strip() or None
        return pd.Series([last_km, last_date, last_dealer])

    df[['last_sbe_km_map', 'last_sbe_date_map', 'last_sbe_dealer_map']] = \
        df.apply(get_last_sbe_map, axis=1)

    aktif = df.get('Kategori', pd.Series(dtype=str))

    return pd.DataFrame({
        'no_rangka':          df['no_rangka'],
        'last_sbe_km_map':    df['last_sbe_km_map'],
        'last_sbe_date_map':  df['last_sbe_date_map'],
        'last_sbe_dealer_map':df['last_sbe_dealer_map'],
        'aktif_kategori':     aktif.str.strip() if aktif is not None else None,
    })


def load_tcare_type_from_nasional() -> pd.DataFrame:
    """Ambil tcare_type + next_service + sisa dari T-CARE Nasional."""
    files = sorted(TCARE_NASIONAL_DIR.glob('*.xlsx'), reverse=True)
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
        'no_rangka':      df['no_rangka'],
        'tcare_type_tc':  df['T-CARE TYPE'].str.strip(),
        'sisa_tc':        pd.to_numeric(df['SISA'], errors='coerce'),
        'next_service_tc':df['Next Service'].str.strip(),
    })


def load_sa_terakhir() -> pd.DataFrame:
    """Ambil SA terakhir per no_rangka dari unitmasuk."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("""
            SELECT no_rangka, sa AS sa_terakhir, tgl_invoice AS tgl_sa_terakhir
            FROM unitmasuk
            WHERE tgl_invoice = (
                SELECT MAX(x.tgl_invoice) FROM unitmasuk x
                WHERE x.no_rangka = unitmasuk.no_rangka
            )
            GROUP BY no_rangka
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
# HITUNG SISA & DETAIL
# ════════════════════════════════════════

def get_tcare_type(row) -> str:
    """Logic tcare_type dari rs."""
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


def calc_sisa(last_km) -> tuple:
    """
    Hitung sisa service dan detailnya.
    Return: (sisa_int, sisa_detail_str, next_service_str)
    """
    if pd.isna(last_km) or last_km is None:
        return 7, '1K, 10K, 20K, 30K, 40K, 50K, 60K', '1ST'
    try:
        km = int(last_km)
    except Exception:
        return None, None, None

    service_ke = KM_TO_SERVICE.get(km)
    if service_ke is None:
        return None, None, None

    sisa = 7 - service_ke
    remaining_kms = [f"{k//1000}K" if k > 1000 else "1K"
                     for k in ALL_KM if KM_TO_SERVICE[k] > service_ke]
    sisa_detail  = ', '.join(remaining_kms) if remaining_kms else 'SELESAI'

    ordinal = {1:'1ST',2:'2ND',3:'3RD',4:'4TH',5:'5TH',6:'6TH',7:'7TH'}
    next_ke  = service_ke + 1
    next_svc = ordinal.get(next_ke, 'NON PPM') if next_ke <= 7 else 'NON PPM'

    return sisa, sisa_detail, next_svc


# ════════════════════════════════════════
# DETEKSI FLAG PENDING SBE
# ════════════════════════════════════════

def detect_pending_sbe(master: pd.DataFrame, df_wo: pd.DataFrame) -> pd.DataFrame:
    """
    Flag unit yang kunjungan non-SBE setelah last SBE → kemungkinan
    SA lupa buat WO SBE.
    """
    if len(df_wo) == 0:
        master['flag_pending_sbe']    = False
        master['flag_sa']             = None
        master['flag_tgl_kunjungan']  = None
        master['flag_wo_type']        = None
        master['next_sbe_expected']   = None
        return master

    flags = []
    for _, row in master.iterrows():
        nr          = row['no_rangka']
        last_sbe_d  = row.get('last_sbe_date')
        sisa        = row.get('sisa_service')
        batas       = row.get('batas_tcare')
        next_sbe    = row.get('next_sbe_expected')

        # Kondisi: masih ada TCARE + belum expired + ada last SBE
        if not (sisa and sisa > 0 and pd.notna(batas)):
            flags.append((False, None, None, None, None))
            continue
        try:
            if pd.to_datetime(batas) < pd.Timestamp.today():
                flags.append((False, None, None, None, None))
                continue
        except Exception:
            flags.append((False, None, None, None, None))
            continue

        # Cari WO non-SBE setelah last_sbe_date
        wo_unit = df_wo[df_wo['no_rangka'] == nr].copy()
        if len(wo_unit) == 0 or pd.isna(last_sbe_d):
            flags.append((False, None, None, None, None))
            continue

        wo_after = wo_unit[wo_unit['tgl_invoice'] > last_sbe_d]
        if len(wo_after) == 0:
            flags.append((False, None, None, None, None))
            continue

        latest = wo_after.sort_values('tgl_invoice', ascending=False).iloc[0]
        flags.append((
            True,
            latest['sa'],
            latest['tgl_invoice'],
            latest['kelompok'],
            next_sbe,
        ))

    flag_df = pd.DataFrame(flags, columns=[
        'flag_pending_sbe', 'flag_sa',
        'flag_tgl_kunjungan', 'flag_wo_type', 'flag_next_sbe'
    ])
    master = master.reset_index(drop=True)
    return pd.concat([master, flag_df], axis=1)


# ════════════════════════════════════════
# RUN
# ════════════════════════════════════════

def run(paths: dict = None):
    print("\n  Load TCARE Unit...")

    # ── Load semua sumber ──
    df_sbe_um  = load_sbe_from_unitmasuk()
    df_sbe_map = load_sbe_from_mapping_cust()
    df_tc_type = load_tcare_type_from_nasional()
    df_sa      = load_sa_terakhir()
    df_wo      = load_wo_non_sbe()

    # ── Ambil no_rangka dari rs ──
    conn = sqlite3.connect(DB_PATH)
    df_rs = pd.read_sql("""
        SELECT no_rangka, dealer_kategori, batas_tcare
        FROM rs
    """, conn)
    # Ambil model untuk derive tcare_type
    try:
        df_model = pd.read_sql("SELECT no_rangka, model FROM rs", conn)
    except Exception:
        df_model = pd.DataFrame()
    conn.close()

    master = df_rs.copy()

    # ── Merge tcare_type dari nasional ──
    if len(df_tc_type) > 0:
        master = master.merge(df_tc_type, on='no_rangka', how='left')

    # ── Merge model ──
    if len(df_model) > 0:
        master = master.merge(df_model, on='no_rangka', how='left')

    # ── Tentukan tcare_type ──
    master['tcare_type'] = master.apply(get_tcare_type, axis=1)

    # ── Merge SBE dari unitmasuk ──
    if len(df_sbe_um) > 0:
        master = master.merge(df_sbe_um, on='no_rangka', how='left')

    # ── Merge SBE dari mapping cust ──
    if len(df_sbe_map) > 0:
        master = master.merge(df_sbe_map, on='no_rangka', how='left')

    # ── Pilih last_sbe_km terbesar dari dua sumber ──
    km_um  = pd.to_numeric(master.get('last_sbe_km_um',  pd.Series(dtype=float)), errors='coerce')
    km_map = pd.to_numeric(master.get('last_sbe_km_map', pd.Series(dtype=float)), errors='coerce')

    master['last_sbe_km'] = km_um.combine(km_map, lambda a, b:
        max(x for x in [a, b] if pd.notna(x)) if any(pd.notna(x) for x in [a, b]) else None
    )

    # Tentukan source
    def get_source(row):
        um  = row.get('last_sbe_km_um')
        mp  = row.get('last_sbe_km_map')
        if pd.isna(um) and pd.isna(mp):   return None
        if pd.isna(um):                    return 'mapping_cust'
        if pd.isna(mp):                    return 'unitmasuk'
        return 'unitmasuk' if um >= mp else 'mapping_cust'

    master['last_sbe_source'] = master.apply(get_source, axis=1)
    master['last_sbe_date']   = master.apply(
        lambda r: r.get('last_sbe_date_um')
        if r.get('last_sbe_source') == 'unitmasuk'
        else r.get('last_sbe_date_map'), axis=1)
    master['last_sbe_dealer'] = master.apply(
        lambda r: r.get('last_sbe_dealer_um')
        if r.get('last_sbe_source') == 'unitmasuk'
        else r.get('last_sbe_dealer_map'), axis=1)

    # ── Hitung sisa, sisa_detail, next_service ──
    sisa_tuples = master['last_sbe_km'].apply(calc_sisa)
    master['sisa_service']     = [t[0] for t in sisa_tuples]
    master['sisa_detail']      = [t[1] for t in sisa_tuples]
    master['next_sbe_expected'] = [t[2] for t in sisa_tuples]

    # Pakai sisa dari T-CARE Nasional kalau lebih fresh dan ada
    if 'sisa_tc' in master.columns:
        mask = master['last_sbe_source'].isin(['mapping_cust', None]) & master['sisa_tc'].notna()
        master.loc[mask, 'sisa_service'] = master.loc[mask, 'sisa_tc']
    if 'next_service_tc' in master.columns:
        mask = master['next_service_tc'].notna() & master['last_sbe_source'].isin(['mapping_cust', None])
        master.loc[mask, 'next_service'] = master.loc[mask, 'next_service_tc']
    else:
        master['next_service'] = master.get('next_sbe_expected')

    # ── Merge SA terakhir ──
    if len(df_sa) > 0:
        master = master.merge(df_sa, on='no_rangka', how='left')

    # ── Deteksi flag pending SBE ──
    master = detect_pending_sbe(master, df_wo)

    # ── Kolom final ──
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
    # Hapus kolom duplikat kalau ada
    master = master.loc[:, ~master.columns.duplicated()]
    master['last_updated'] = datetime.now().strftime('%Y-%m-%d')

    # ── Simpan ke DB ──
    conn = sqlite3.connect(DB_PATH)
    master.to_sql('tcare_unit', conn, if_exists='replace', index=False)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tcu_rangka ON tcare_unit(no_rangka)')
    conn.commit()
    conn.close()

    flag_n = master['flag_pending_sbe'].sum() if 'flag_pending_sbe' in master.columns else 0
    print(f"  ✅ tcare_unit: {len(master):,} unit ({int(flag_n)} pending SBE flag)")


if __name__ == '__main__':
    run()