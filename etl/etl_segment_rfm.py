"""
etl_segment_rfm.py
====================
Hitung segmentasi RFM (Champion/Loyal/Potential/New/At Risk/Lost)
berdasarkan customer_profile.

Threshold (standard kedatangan 2.2x/tahun = interval ideal ~166 hari):
  Recency ideal : <= 166 hari
  Recency lost  : > 300 hari
  Revenue median: Rp 914.000

Harus dijalankan SETELAH etl_customer_profile.py

Cara pakai:
  python etl_segment_rfm.py
"""

import sqlite3
import pandas as pd

DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"

RECENCY_IDEAL  = 166
RECENCY_LOST   = 300
MEDIAN_REVENUE = 914000


def segmentasi(row):
    freq = row['total_kunjungan_fisik']
    rec  = row['hari_sejak_kunjungan']
    rev  = row['avg_revenue_per_wo']

    if rec > RECENCY_LOST:
        return 'Lost'
    if rec > RECENCY_IDEAL:
        return 'At Risk'
    if freq == 1:
        return 'New'
    if freq in (2, 3):
        return 'Potential'
    if rev >= MEDIAN_REVENUE:
        return 'Champion'
    return 'Loyal'


def run():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("""
        SELECT no_rangka, hari_sejak_kunjungan, total_kunjungan_fisik, avg_revenue_per_wo
        FROM customer_profile
        WHERE tgl_kunjungan_terakhir IS NOT NULL
    """, conn)

    df['segment_rfm'] = df.apply(segmentasi, axis=1)

    updated = 0
    for _, row in df.iterrows():
        conn.execute(
            "UPDATE customer_profile SET segment_rfm = ? WHERE no_rangka = ?",
            (row['segment_rfm'], row['no_rangka'])
        )
        updated += 1
    conn.commit()

    print(f"segment_rfm updated: {updated:,} rows")
    print(f"\nDistribusi segment_rfm:")
    print(df['segment_rfm'].value_counts())

    conn.close()


if __name__ == '__main__':
    run()
