"""
etl_master_adt.py
==================
Load master ADT dari Excel ke tabel master_adt.
Sumber: D:\Database Unit Masuk\Master\master_adt.xlsx

Cara pakai:
  python etl_master_adt.py
"""

import sqlite3
import pandas as pd

DB_PATH     = r"D:\AI_nasmoco\db\nasmoco.db"
MASTER_PATH = r"D:\Database Unit Masuk\Master\master_adt.xlsx"


def run():
    df = pd.read_excel(MASTER_PATH, dtype={'kode_item': str, 'nama_canonical': str})
    df['aktif'] = df['aktif'].fillna(1).astype(int)
    df = df.drop_duplicates(subset='kode_item')
    df = df[['kode_item', 'nama_canonical', 'aktif']]

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS master_adt (
            kode_item       TEXT PRIMARY KEY,
            nama_canonical  TEXT NOT NULL,
            aktif           INTEGER DEFAULT 1
        )
    """)
    df.to_sql('master_adt', conn, if_exists='replace', index=False)
    conn.commit()

    count = pd.read_sql("SELECT COUNT(*) as n FROM master_adt", conn).iloc[0]['n']
    print(f"master_adt: {count} rows loaded")
    conn.close()


if __name__ == '__main__':
    run()
