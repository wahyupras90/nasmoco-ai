import sqlite3
import pandas as pd
import time
import os
from config import DB_PATH


def run_query(sql):

    DEBUG = os.environ.get('NASMOCO_DEBUG', '0') == '1'

    if DEBUG:
        print("\nEXECUTING SQL...")
        print(sql)

    start = time.time()

    conn = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro",
        uri=True
    )

    df = pd.read_sql_query(sql, conn)
    conn.close()

    elapsed = time.time() - start

    if DEBUG:
        print(f"\nSQL EXECUTION = {elapsed:.2f} detik")
        print("\nQUERY SELESAI")

    return df