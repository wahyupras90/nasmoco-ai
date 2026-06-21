import sqlite3, pandas as pd

conn = sqlite3.connect(r"D:\AI_nasmoco\db\nasmoco.db")

# Cek periode tersedia
df = pd.read_sql("""
    SELECT strftime('%Y-%m', tanggal) as bulan,
           COUNT(*) as rows,
           SUM(cpus) as total_cpus,
           ROUND(SUM(revenue)) as total_revenue
    FROM daily_kpi
    WHERE is_counter = 0
    GROUP BY bulan ORDER BY bulan
""", conn)
print(df.to_string())
conn.close()