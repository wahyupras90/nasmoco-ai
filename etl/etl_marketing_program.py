"""
etl_marketing_program.py
===========================
Buat tabel marketing_program (master rule program CRM) dan seed 4 program
default. Tabel ini di-rebuild setiap kali dijalankan (DROP & recreate),
jadi program manual yang ditambahkan via UI/DB editor di luar 4 default
ini akan hilang saat di-rerun -- gunakan dengan hati-hati di pipeline rutin.

Fokus 4 program: dorong customer KEMBALI DATANG (bukan upselling produk
spesifik -- itu domain SA saat customer sudah hadir).

Cara pakai:
  python etl_marketing_program.py
"""

import sqlite3
import pandas as pd
from datetime import date

DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"


def create_marketing_program_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS marketing_program")
    conn.execute("""
        CREATE TABLE marketing_program (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            nama_program    TEXT NOT NULL,
            deskripsi       TEXT,
            kondisi_sql     TEXT NOT NULL,
            prioritas       TEXT,
            aktif           INTEGER DEFAULT 1,
            created_at      TEXT
        )
    """)
    conn.commit()
    conn.close()


def seed_programs():
    today = date.today().isoformat()
    programs = [
        (
            "Panggil Pulang - At Risk",
            "Customer mulai jarang, belum lost. Interval sudah lewat ideal (166 hari) tapi belum 300 hari.",
            "segment_rfm = 'At Risk'",
            "HIGH",
            1,
            today
        ),
        (
            "Panggil Pulang - Lost",
            "Customer sudah lebih dari 300 hari tidak datang. Prioritas reaktivasi.",
            "segment_rfm = 'Lost'",
            "HIGH",
            1,
            today
        ),
        (
            "Percepat Interval - Loyal Customer",
            "Customer reguler (Champion/Loyal) yang sudah lewat prediksi kunjungan berikutnya.",
            "segment_rfm IN ('Champion','Loyal') AND hari_sejak_kunjungan > interval_avg_hari",
            "MEDIUM",
            1,
            today
        ),
        (
            "Aktivasi New & Potential",
            "Customer baru 1-3x kunjungan, dorong jadi reguler sebelum sempat hilang.",
            "segment_rfm IN ('New','Potential') AND hari_sejak_kunjungan > 150",
            "MEDIUM",
            1,
            today
        ),
    ]

    conn = sqlite3.connect(DB_PATH)
    conn.executemany("""
        INSERT INTO marketing_program 
        (nama_program, deskripsi, kondisi_sql, prioritas, aktif, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, programs)
    conn.commit()

    df = pd.read_sql("SELECT * FROM marketing_program", conn)
    print(df.to_string())
    conn.close()


def run():
    create_marketing_program_table()
    seed_programs()


if __name__ == '__main__':
    run()
