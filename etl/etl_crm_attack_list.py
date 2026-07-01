"""
etl_crm_attack_list.py
=========================
Generate crm_attack_list dengan menjalankan kondisi_sql tiap program aktif
di marketing_program terhadap customer_profile.

CATATAN PENTING -- ini BERBEDA dari "attack list TCARE" (tools/attack_list_tcare.py)
yang bersumber dari tcare_schedule (jadwal servis TCARE bulanan, status pending).
crm_attack_list ini fokus ke perilaku customer secara umum (RFM segmentation)
untuk program "panggil pulang" lintas semua kategori servis, bukan hanya TCARE.

Harus dijalankan SETELAH:
  1. etl_customer_profile.py
  2. etl_segment_rfm.py
  3. etl_marketing_program.py

Cara pakai:
  python etl_crm_attack_list.py
"""

import sqlite3
import pandas as pd
from datetime import date

DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"


def create_crm_attack_list_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS crm_attack_list")
    conn.execute("""
        CREATE TABLE crm_attack_list (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            no_rangka       TEXT,
            customer        TEXT,
            model           TEXT,
            segment         TEXT,
            segment_rfm     TEXT,
            sa_terakhir     TEXT,
            tgl_kunjungan_terakhir TEXT,
            hari_sejak_kunjungan   INTEGER,
            interval_avg_hari      REAL,
            avg_revenue_per_wo     REAL,
            pct_wo_with_adt        REAL,
            pct_wo_with_sublet     REAL,
            program_id      INTEGER,
            nama_program    TEXT,
            alasan          TEXT,
            status          TEXT DEFAULT 'pending',
            tgl_generate    TEXT,
            tgl_followup    TEXT,
            FOREIGN KEY (program_id) REFERENCES marketing_program(id)
        )
    """)
    conn.commit()
    conn.close()


def generate_alasan(row):
    parts = []
    if pd.notna(row['hari_sejak_kunjungan']):
        parts.append(f"{int(row['hari_sejak_kunjungan'])} hari sejak kunjungan terakhir")
    if pd.notna(row['avg_revenue_per_wo']):
        parts.append(f"avg revenue Rp{int(row['avg_revenue_per_wo']):,}")
    # ADT dan sublet adalah kesatuan upselling
    no_adt    = pd.notna(row['pct_wo_with_adt'])    and row['pct_wo_with_adt'] == 0
    no_sublet = pd.notna(row['pct_wo_with_sublet']) and row['pct_wo_with_sublet'] == 0
    if no_adt and no_sublet:
        parts.append("belum bisa di-upselling")
    return ", ".join(parts)


def run():
    conn = sqlite3.connect(DB_PATH)
    programs = pd.read_sql("SELECT * FROM marketing_program WHERE aktif = 1", conn)

    today = date.today().isoformat()
    all_rows = []

    for _, prog in programs.iterrows():
        query = f"""
            SELECT no_rangka, customer, model, segment, segment_rfm, sa_terakhir,
                   tgl_kunjungan_terakhir, hari_sejak_kunjungan, interval_avg_hari,
                   avg_revenue_per_wo, pct_wo_with_adt, pct_wo_with_sublet
            FROM customer_profile
            WHERE {prog['kondisi_sql']}
        """
        df = pd.read_sql(query, conn)
        df['program_id']   = prog['id']
        df['nama_program'] = prog['nama_program']
        df['alasan']       = df.apply(generate_alasan, axis=1)
        df['status']       = 'pending'
        df['tgl_generate'] = today
        df['tgl_followup'] = None

        print(f"{prog['nama_program']:40s}: {len(df):,} unit")
        all_rows.append(df)

    df_final = pd.concat(all_rows, ignore_index=True)

    kolom = [
        'no_rangka','customer','model','segment','segment_rfm','sa_terakhir',
        'tgl_kunjungan_terakhir','hari_sejak_kunjungan','interval_avg_hari',
        'avg_revenue_per_wo','pct_wo_with_adt','pct_wo_with_sublet',
        'program_id','nama_program','alasan','status','tgl_generate','tgl_followup'
    ]
    df_final = df_final[kolom]
    df_final.to_sql('crm_attack_list', conn, if_exists='append', index=False)
    conn.commit()

    print(f"\nTotal crm_attack_list: {len(df_final):,} rows")
    conn.close()


if __name__ == '__main__':
    create_crm_attack_list_table()
    run()