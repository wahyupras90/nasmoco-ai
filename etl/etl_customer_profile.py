"""
etl_customer_profile.py
========================
Bangun tabel customer_profile dari data kunjungan (rekapbulanan + unitmasuk +
bufferparts + master_adt), dengan deduplikasi kunjungan fisik TCARE
(anchor WO TCARE, pair GRP/LUB -7 hari, CLAIM PART +7 hari).

Base table customer_profile = union dari rs + unit BERKAH yang punya transaksi
valid tapi tidak ada di rs (segment di-derive dari mapping model exact match,
fallback ke keyword matching).

Cara pakai:
  python etl_customer_profile.py
"""

import sqlite3
import pandas as pd
from datetime import date

DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"

KEYWORD_SEGMENT = {
    'AGYA':          'New Entry',
    'CALYA':         'New Entry',
    'ETIOS':         'New Entry',
    'AVANZA':        'Economy',
    'RUSH':          'Economy',
    'RAIZE':         'Economy',
    'VELOZ':         'Economy',
    'VIOS':          'Standard',
    'YARIS':         'Standard',
    'KIJANG INNOVA': 'Standard',
    'INNOVA':        'Standard',
    'KIJANG':        'Standard',
    'SIENTA':        'Standard',
    'HILUX':         'Standard',
    'HIACE':         'Standard',
    'HI ACE':        'Standard',
    'HI-LUX':        'Standard',
    'FORTUNER':      'Medium Luxury',
    'COROLLA':       'Medium Luxury',
    'ALTIS':         'Medium Luxury',
    'VOXY':          'Medium Luxury',
    'CAMRY':         'Luxury',
    'ALPHARD':       'Luxury',
    'VELLFIRE':      'Luxury',
    'VELFIRE':       'Luxury',
    'LAND CRUISER':  'Luxury',
    'LANDCRUISER':   'Luxury',
    'LEXUS':         'Luxury',
    'HARRIER':       'Luxury',
    'DYNA':          'Commercial',
}


def match_segment_by_keyword(model):
    if pd.isna(model):
        return 'Other'
    model_upper = str(model).upper()
    for keyword, segment in KEYWORD_SEGMENT.items():
        if keyword in model_upper:
            return segment
    return 'Other'


def load_base():
    conn = sqlite3.connect(DB_PATH)

    df_rs = pd.read_sql("""
        SELECT no_rangka, customer, model, segment, dealer_kategori
        FROM rs
        WHERE no_rangka IS NOT NULL AND no_rangka != ''
    """, conn)

    df_extra = pd.read_sql("""
        SELECT 
            u.no_rangka,
            MAX(u.customer) as customer,
            MAX(u.model) as model,
            'BERKAH' as dealer_kategori
        FROM unitmasuk u
        JOIN rekapbulanan r ON u.no_wo = r.no_wo
        WHERE u.no_rangka IS NOT NULL AND u.no_rangka != ''
          AND r.invoice > 50000
          AND u.no_rangka NOT IN (SELECT no_rangka FROM rs)
        GROUP BY u.no_rangka
    """, conn)
    conn.close()

    df_map = df_rs.dropna(subset=['model']).groupby(['model', 'segment']).size().reset_index(name='cnt')
    model_to_segment = (
        df_map.sort_values('cnt', ascending=False)
        .drop_duplicates('model')
        .set_index('model')['segment'].to_dict()
    )

    df_extra['segment'] = df_extra['model'].map(model_to_segment)
    mask_missing = df_extra['segment'].isna()
    df_extra.loc[mask_missing, 'segment'] = df_extra.loc[mask_missing, 'model'].apply(match_segment_by_keyword)

    df_base = pd.concat([df_rs, df_extra], ignore_index=True)
    df_base = df_base.drop_duplicates(subset='no_rangka', keep='first')

    print(f"Dari rs    : {len(df_rs):,}")
    print(f"Extra      : {len(df_extra):,}")
    print(f"Total base : {len(df_base):,}")
    return df_base


def rebuild_customer_profile_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS customer_profile")
    conn.execute("""
        CREATE TABLE customer_profile (
            no_rangka                TEXT PRIMARY KEY,
            customer                 TEXT,
            model                    TEXT,
            segment                  TEXT,
            dealer_kategori          TEXT,
            sa_terakhir              TEXT,
            tgl_kunjungan_terakhir   TEXT,
            hari_sejak_kunjungan     INTEGER,
            total_wo_valid           INTEGER,
            total_kunjungan_fisik    INTEGER,
            interval_avg_hari        REAL,
            interval_last_hari       INTEGER,
            predicted_next_visit     TEXT,
            avg_revenue_per_wo       REAL,
            total_revenue_lifetime   REAL,
            total_revenue_cash       REAL,
            avg_revenue_total        REAL,
            avg_tgp_per_wo           REAL,
            avg_adt_per_wo           REAL,
            avg_sublet_per_wo        REAL,
            pct_wo_with_tgp          REAL,
            pct_wo_with_adt          REAL,
            pct_wo_with_sublet       REAL,
            last_pekerjaan           TEXT,
            total_wo_tcare           INTEGER,
            total_wo_upselling       INTEGER,
            segment_rfm              TEXT,
            last_updated              TEXT
        )
    """)
    conn.commit()
    conn.close()


def insert_base(df_base):
    conn = sqlite3.connect(DB_PATH)
    df_base.to_sql('customer_profile', conn, if_exists='append', index=False)
    conn.commit()
    count = pd.read_sql("SELECT COUNT(*) as n FROM customer_profile", conn).iloc[0]['n']
    conn.close()
    print(f"customer_profile base inserted: {count:,} rows")


def load_kunjungan():
    conn = sqlite3.connect(DB_PATH)

    df = pd.read_sql("""
        SELECT 
            u.no_wo, u.no_rangka, u.tanggal, u.sa, u.pekerjaan,
            u.kategori, u.kelompok, u.tcare,
            r.invoice, r.jasa, r.sublet, r.tgp
        FROM (
            SELECT no_wo, no_rangka, tanggal, sa, pekerjaan,
                   kategori, kelompok, tcare,
                   ROW_NUMBER() OVER (
                       PARTITION BY no_wo
                       ORDER BY CASE tcare WHEN 'TCARE' THEN 0 ELSE 1 END, rowid
                   ) AS rn
            FROM unitmasuk
            WHERE no_rangka IS NOT NULL AND no_rangka != ''
        ) u
        JOIN rekapbulanan r ON u.no_wo = r.no_wo
        WHERE u.rn = 1 AND r.invoice > 50000
    """, conn)

    df_sublet = pd.read_sql("""
        SELECT no_wo, GROUP_CONCAT(DISTINCT sublet_type) as sublet_items
        FROM unitmasuk
        WHERE sublet_type IS NOT NULL AND sublet_type != ''
        GROUP BY no_wo
    """, conn)

    df_adt = pd.read_sql("""
        SELECT b.no_wo, SUM(b.invoice) as adt_revenue
        FROM bufferparts b
        JOIN master_adt m ON b.kode_item = m.kode_item
        WHERE m.aktif = 1
        GROUP BY b.no_wo
    """, conn)
    conn.close()

    df['no_wo'] = df['no_wo'].astype('int64')
    df_sublet = df_sublet[df_sublet['no_wo'].notna() & (df_sublet['no_wo'] != '')]
    df_sublet['no_wo'] = df_sublet['no_wo'].astype('int64')
    df_adt = df_adt[df_adt['no_wo'].notna() & (df_adt['no_wo'] != '')]
    df_adt['no_wo'] = df_adt['no_wo'].astype('int64')

    df = df.merge(df_sublet, on='no_wo', how='left')
    df = df.merge(df_adt, on='no_wo', how='left')
    df['adt_revenue'] = df['adt_revenue'].fillna(0)
    df['sublet_items'] = df['sublet_items'].fillna('')
    df['tanggal'] = pd.to_datetime(df['tanggal'])

    CLAIM_KEYWORDS = ['CLAIM PART T-CARE', 'CLAIM PART T-CATE', 'PEMAKAIAN PART TCARE']
    df['is_claim'] = df['pekerjaan'].str.upper().apply(
        lambda x: any(k in x for k in CLAIM_KEYWORDS) if pd.notna(x) else False
    )

    df = df.sort_values(['no_rangka', 'tanggal']).reset_index(drop=True)
    df['cluster_id'] = range(len(df))
    df['tgl_kunjungan'] = df['tanggal']
    df['is_paired'] = False

    for nrk, grp in df.groupby('no_rangka'):
        grp = grp.sort_values('tanggal')
        tcare_rows = grp[grp['tcare'] == 'TCARE'].index.tolist()
        paired_idx = set()

        for tcare_idx in tcare_rows:
            tcare_tgl = df.loc[tcare_idx, 'tanggal']
            cluster = df.loc[tcare_idx, 'cluster_id']

            kandidat_pre = grp[
                (grp['tcare'] == 'REGULER') &
                (grp['kelompok'].isin(['GRP', 'LUB'])) &
                (grp['tanggal'] >= tcare_tgl - pd.Timedelta(days=7)) &
                (grp['tanggal'] <= tcare_tgl) &
                (~grp.index.isin(paired_idx))
            ].sort_values('tanggal', ascending=False)

            if not kandidat_pre.empty:
                pre_idx = kandidat_pre.index[0]
                df.loc[pre_idx, 'cluster_id'] = cluster
                df.loc[pre_idx, 'is_paired'] = True
                df.loc[tcare_idx, 'tgl_kunjungan'] = df.loc[pre_idx, 'tanggal']
                paired_idx.add(pre_idx)

            kandidat_post = grp[
                (grp['is_claim'] == True) &
                (grp['tanggal'] >= tcare_tgl) &
                (grp['tanggal'] <= tcare_tgl + pd.Timedelta(days=7)) &
                (~grp.index.isin(paired_idx))
            ].sort_values('tanggal')

            if not kandidat_post.empty:
                post_idx = kandidat_post.index[0]
                df.loc[post_idx, 'cluster_id'] = cluster
                df.loc[post_idx, 'is_paired'] = True
                paired_idx.add(post_idx)

    df_anchor = df[df.index == df['cluster_id']].copy()

    rev_total = df.groupby('cluster_id')['invoice'].sum().rename('rev_cluster_total')
    rev_cash = df[~df['is_claim'] & (df['tcare'] != 'TCARE')].groupby(
        'cluster_id')['invoice'].sum().rename('rev_cluster_cash')
    sublet_rev = df.groupby('cluster_id')['sublet'].sum().rename('sublet_cluster')
    tgp_rev = df.groupby('cluster_id')['tgp'].sum().rename('tgp_cluster')
    adt_rev = df.groupby('cluster_id')['adt_revenue'].sum().rename('adt_cluster')

    df_anchor = df_anchor.join(rev_total, on='cluster_id')
    df_anchor = df_anchor.join(rev_cash, on='cluster_id')
    df_anchor = df_anchor.join(sublet_rev, on='cluster_id')
    df_anchor = df_anchor.join(tgp_rev, on='cluster_id')
    df_anchor = df_anchor.join(adt_rev, on='cluster_id')
    df_anchor['rev_cluster_cash'] = df_anchor['rev_cluster_cash'].fillna(0)

    return df_anchor


def agregasi_per_unit(df_anchor):
    today = pd.Timestamp(date.today())
    grp = df_anchor.groupby('no_rangka')

    agg = pd.DataFrame()
    agg['total_kunjungan_fisik'] = grp['tgl_kunjungan'].count()
    agg['total_wo_valid'] = grp['no_wo'].count()
    agg['tgl_kunjungan_terakhir'] = grp['tgl_kunjungan'].max()
    agg['sa_terakhir'] = grp.apply(lambda x: x.loc[x['tgl_kunjungan'].idxmax(), 'sa'])
    agg['last_pekerjaan'] = grp.apply(lambda x: x.loc[x['tgl_kunjungan'].idxmax(), 'kelompok'])
    agg['total_wo_tcare'] = grp['tcare'].apply(lambda x: (x == 'TCARE').sum())
    agg['total_wo_upselling'] = grp.apply(
        lambda x: ((x['tcare'] == 'TCARE') & (x['rev_cluster_cash'] > 0)).sum()
    )

    agg['total_revenue_lifetime'] = grp['rev_cluster_total'].sum()
    agg['total_revenue_cash'] = grp['rev_cluster_cash'].sum()
    agg['avg_revenue_per_wo'] = agg['total_revenue_cash'] / agg['total_kunjungan_fisik']
    agg['avg_revenue_total'] = agg['total_revenue_lifetime'] / agg['total_kunjungan_fisik']
    agg['avg_tgp_per_wo'] = grp['tgp_cluster'].sum() / agg['total_kunjungan_fisik']
    agg['avg_adt_per_wo'] = grp['adt_cluster'].sum() / agg['total_kunjungan_fisik']
    agg['avg_sublet_per_wo'] = grp['sublet_cluster'].sum() / agg['total_kunjungan_fisik']

    agg['pct_wo_with_tgp'] = grp['tgp_cluster'].apply(lambda x: (x > 0).mean()) * 100
    agg['pct_wo_with_adt'] = grp['adt_cluster'].apply(lambda x: (x > 0).mean()) * 100
    agg['pct_wo_with_sublet'] = grp['sublet_cluster'].apply(lambda x: (x > 0).mean()) * 100

    def hitung_interval(dates):
        dates = dates.sort_values()
        if len(dates) < 2:
            return pd.Series({'interval_avg': None, 'interval_last': None})
        gaps = dates.diff().dropna().dt.days
        return pd.Series({
            'interval_avg': round(gaps.mean(), 1),
            'interval_last': int((today - dates.iloc[-1]).days)
        })

    interval = grp['tgl_kunjungan'].apply(hitung_interval).unstack()
    agg['interval_avg_hari'] = interval['interval_avg']
    agg['interval_last_hari'] = interval['interval_last']
    agg['hari_sejak_kunjungan'] = (today - agg['tgl_kunjungan_terakhir']).dt.days
    agg['predicted_next_visit'] = (
        agg['tgl_kunjungan_terakhir'] +
        pd.to_timedelta(agg['interval_avg_hari'].fillna(0), unit='D')
    )

    agg = agg.reset_index()
    agg['last_updated'] = date.today().isoformat()
    agg['tgl_kunjungan_terakhir'] = agg['tgl_kunjungan_terakhir'].dt.strftime('%Y-%m-%d')
    agg['predicted_next_visit'] = agg['predicted_next_visit'].dt.strftime('%Y-%m-%d')

    print(f"Agregasi selesai : {len(agg):,} unit")
    return agg


def update_customer_profile(df_agg):
    conn = sqlite3.connect(DB_PATH)
    kolom_update = [
        'sa_terakhir', 'tgl_kunjungan_terakhir', 'hari_sejak_kunjungan',
        'total_wo_valid', 'total_kunjungan_fisik', 'interval_avg_hari',
        'interval_last_hari', 'predicted_next_visit', 'avg_revenue_per_wo',
        'avg_revenue_total', 'total_revenue_lifetime', 'total_revenue_cash',
        'avg_tgp_per_wo', 'avg_adt_per_wo', 'avg_sublet_per_wo',
        'pct_wo_with_tgp', 'pct_wo_with_adt', 'pct_wo_with_sublet',
        'last_pekerjaan', 'total_wo_tcare', 'total_wo_upselling', 'last_updated'
    ]
    updated = 0
    for _, row in df_agg.iterrows():
        sets = ', '.join([f"{k} = ?" for k in kolom_update])
        values = [row.get(k) for k in kolom_update] + [row['no_rangka']]
        conn.execute(f"UPDATE customer_profile SET {sets} WHERE no_rangka = ?", values)
        updated += 1
    conn.commit()
    conn.close()
    print(f"customer_profile updated: {updated:,} rows")


def run():
    print("Step 1: Rebuild tabel...")
    rebuild_customer_profile_table()

    print("\nStep 2: Load base (rs + unit BERKAH tanpa rs)...")
    df_base = load_base()
    insert_base(df_base)

    print("\nStep 3: Load & cluster kunjungan...")
    df_anchor = load_kunjungan()
    print(f"  -> {len(df_anchor):,} kunjungan fisik, {df_anchor['no_rangka'].nunique():,} unit")

    print("\nStep 4: Agregasi per unit...")
    df_agg = agregasi_per_unit(df_anchor)

    print("\nStep 5: Update customer_profile...")
    update_customer_profile(df_agg)

    print("\ncustomer_profile selesai!")


if __name__ == '__main__':
    run()
