import sqlite3, pandas as pd
conn = sqlite3.connect(r"D:\AI_nasmoco\db\nasmoco.db")
df = pd.read_sql("""
    SELECT r.no_wo, u.tanggal AS tgl_masuk, r.tanggal AS tgl_invoice,
           r.total_revenue
    FROM rekapbulanan r
    JOIN unitmasuk u ON r.no_wo = u.no_wo
    WHERE r.sa = 'ZKY'
      AND strftime('%Y-%m', u.tanggal) != strftime('%Y-%m', r.tanggal)
    LIMIT 10
""", conn)
print(df.to_string())
conn.close()