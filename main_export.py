from ai.openrouter_client import ask_ai
from db.query import run_query
from pathlib import Path
import pandas as pd
import re
from datetime import datetime
import argparse
import os


# ════════════════════════════════════════
# LOAD PROMPTS — sekali di luar loop
# ════════════════════════════════════════

SQL_PROMPT = Path("ai/sql_prompt.txt").read_text(encoding="utf-8")
SYSTEM_PROMPT = Path("ai/system_prompt.txt").read_text(encoding="utf-8")




# ════════════════════════════════════════
# ROUTING
# ════════════════════════════════════════

ANALYSIS_KEYWORDS = [
    "analisa", "analysis", "kenapa", "mengapa",
    "penyebab", "root cause", "insight",
    "rekomendasi", "warning", "bandingkan",
    "compare", "trend", "growth", "evaluasi"
]

LAPORAN_KEYWORDS = [
    "buat laporan", "generate laporan",
    "laporan bulan", "buat report",
    "generate report", "cetak laporan"
]

def need_analysis(text: str) -> bool:
    return any(w in text.lower() for w in ANALYSIS_KEYWORDS)

def need_report(text: str) -> bool:
    return any(w in text.lower() for w in LAPORAN_KEYWORDS)

EXCEL_KEYWORDS = [
    "export excel", "export ke excel", "simpan excel",
    "simpan ke excel", "download excel", "ke excel",
    "export", "unduh excel"
]

def need_export(text: str) -> bool:
    return any(w in text.lower() for w in EXCEL_KEYWORDS)


# ════════════════════════════════════════
# CLEAN SQL
# ════════════════════════════════════════

FORBIDDEN_SQL = [
    "DROP","DELETE","UPDATE","INSERT",
    "ALTER","TRUNCATE","CREATE","REPLACE"
]

def clean_sql(raw: str) -> str:
    """Bersihkan output AI → SQL bersih."""
    sql = str(raw)

    # 1. Strip markdown
    sql = re.sub(r"```sql", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"```", "", sql)

    # 2. Hapus baris komentar saja (-- dan #)
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--") or stripped.startswith("#"):
            continue
        lines.append(line)
    sql = "\n".join(lines)

    # 3. Ambil dari SELECT atau WITH (lebih aman dari regex)
    upper = sql.upper()
    pos   = upper.find("SELECT")
    pos_w = upper.find("WITH")

    if pos_w >= 0 and (pos < 0 or pos_w < pos):
        sql = sql[pos_w:]
    elif pos >= 0:
        sql = sql[pos:]

    # 4. Pastikan ada semicolon
    sql = sql.strip()
    if sql and not sql.endswith(";"):
        sql += ";"

    return sql


def guard_sql(sql: str) -> str | None:
    """Return pesan error kalau SQL berbahaya, None kalau aman."""
    upper = sql.upper()
    for kw in FORBIDDEN_SQL:
        if re.search(r'\b' + kw + r'\b', upper):
            return f"Query diblokir: mengandung '{kw}'"
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        return "Query diblokir: harus dimulai dengan SELECT atau WITH"
    return None










# ════════════════════════════════════════
# SQL VALIDATOR — logic check
# ════════════════════════════════════════

OUTLET_KEYWORDS = [
    "outlet", "dealer", "nasmoco",
    "bengkel", "workshop", "cabang",
    "total outlet", "total bengkel",
]

def validate_sql(sql: str, pertanyaan: str) -> str | None:
    """
    Cek logika bisnis SQL sebelum dieksekusi.
    Return pesan error kalau ada masalah, None kalau aman.
    """
    sql_upper   = sql.upper()
    tanya_lower = pertanyaan.lower()

    # Rule 1: outlet tapi masih GROUP BY sa
    is_outlet   = any(k in tanya_lower for k in OUTLET_KEYWORDS)
    has_group_sa = bool(re.search(
        r'GROUP\s+BY[^;]*\bSA\b', sql_upper
    ))
    if is_outlet and has_group_sa:
        return (
            "⚠ Query outlet tidak boleh GROUP BY sa.\n"
            "Gunakan SUM global tanpa GROUP BY."
        )

    # Rule 2: target/pencapaian tapi tidak ada target_bulanan
    is_target = bool(re.search(
        r'(target|pencapaian|achievement|ach|vs\s*target)',
        tanya_lower
    ))
    has_target_table = "TARGET_BULANAN" in sql_upper
    if is_target and not has_target_table:
        return (
            "⚠ Pertanyaan tentang target tapi query tidak JOIN target_bulanan.\n"
            "Coba ulangi pertanyaan dengan lebih spesifik."
        )

    # Rule 3: tcare tapi tidak pakai rs atau unitmasuk (warning saja)
    is_tcare = any(k in tanya_lower for k in [
        "tcare", "expired", "expiry", "habis tcare", "batas tcare"
    ])
    has_tcare_tables = "RS" in sql_upper or "UNITMASUK" in sql_upper
    if is_tcare and not has_tcare_tables:
        print("⚠ WARNING: pertanyaan TCARE tapi tidak ada tabel rs/unitmasuk.")

    return None


# ════════════════════════════════════════
# FORMAT HASIL
# ════════════════════════════════════════

PCT_KEYWORDS = ['pct', 'persen', '%', 'ratio', 'rate']

def format_hasil(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "Tidak ada data"
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            is_pct = any(k in col.lower() for k in PCT_KEYWORDS)
            if is_pct:
                df[col] = df[col].apply(
                    lambda x: f"{x:,.1f}" if pd.notna(x) else "-"
                )
            else:
                df[col] = df[col].apply(
                    lambda x: f"{x:,.0f}" if pd.notna(x) else "-"
                )
    return df.to_string(index=False)


# ════════════════════════════════════════
# EXPORT EXCEL
# ════════════════════════════════════════

def export_excel(df: pd.DataFrame,
                 pertanyaan: str,
                 output_base: str = "Output") -> str:
    """
    Simpan DataFrame ke Excel.
    Nama file otomatis dari topik + tanggal.
    """
    # Buat nama file dari keyword pertanyaan
    keywords = {
        "tcare":     "tcare",
        "rs":        "rs",
        "revenue":   "revenue",
        "cpus":      "cpus",
        "sa":        "sa",
        "ranking":   "ranking",
        "liter":     "liter",
        "upselling": "upselling",
        "target":    "target",
        "rush":      "model_rush",
        "avanza":    "model_avanza",
        "agya":      "model_agya",
    }
    p = pertanyaan.lower()
    topik = next((v for k, v in keywords.items() if k in p), "data")
    tanggal = datetime.now().strftime("%Y-%m-%d")
    filename = f"{topik}_{tanggal}.xlsx"

    # Buat folder kalau belum ada
    folder = Path(output_base) / "export"
    folder.mkdir(parents=True, exist_ok=True)
    filepath = folder / filename

    # Export dengan format angka yang rapi
    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Data', index=False)

        # Auto-width kolom
        ws = writer.sheets['Data']
        for col in ws.columns:
            max_len = max(
                len(str(cell.value)) if cell.value else 0
                for cell in col
            )
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    return str(filepath)


# ════════════════════════════════════════
# BUILD PROMPTS
# ════════════════════════════════════════

def build_sql_prompt(pertanyaan: str) -> str:
    return f"""
{SQL_PROMPT}

Pertanyaan user:
{pertanyaan}

TUGAS:
Buat 1 query SQL saja.
"""

def build_analyst_prompt(pertanyaan: str,
                          hasil_str: str) -> str:
    """Analyst tidak perlu tahu SQL — cukup pertanyaan dan hasil."""
    return f"""Anda adalah analyst bengkel Toyota Nasmoco Tegal.

Pertanyaan:
{pertanyaan}

Hasil data:
{hasil_str}

Buat analisa dengan format:

FAKTA DATA
(angka utama dari hasil)

TEMUAN
(pola atau anomali)

HIPOTESIS
(jika ada — beri label HIPOTESIS)

REKOMENDASI
(aksi konkret)

Ringkas. Jangan mengarang. Jangan asumsikan perilaku SA.
"""


# ════════════════════════════════════════
# LAPORAN GENERATOR
# ════════════════════════════════════════

def handle_laporan(pertanyaan: str):
    """Generate laporan HTML tanpa AI."""
    try:
        from tools.report_generator import buat_laporan

        tahun, bulan = None, None
        bulan_map = {
            'januari':1,'februari':2,'maret':3,'april':4,
            'mei':5,'juni':6,'juli':7,'agustus':8,
            'september':9,'oktober':10,'november':11,'desember':12
        }
        p = pertanyaan.lower()
        for nama, num in bulan_map.items():
            if nama in p:
                bulan = num
                break

        yr = re.search(r'\b(202\d)\b', pertanyaan)
        if yr:
            tahun = int(yr.group(1))

        print("\n  Generating laporan HTML...")
        filepath = buat_laporan(tahun, bulan)
        print(f"\nLAPORAN SELESAI:\n  {filepath}")
        print("Buka di browser atau kirim ke grup WA.\n")

    except ImportError:
        print("\n⚠ report_generator.py tidak ditemukan di tools/")
    except Exception as e:
        print(f"\nERROR generate laporan: {e}")


# ════════════════════════════════════════
# MAIN LOOP
# ════════════════════════════════════════

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='Tampilkan detail SQL dan proses')
    args = parser.parse_args()
    DEBUG = args.debug

    print("\n" + "═" * 42)
    print("  AI Nasmoco Analyst" + (" [DEBUG]" if DEBUG else ""))
    print("═" * 42)
    print("ketik 'exit' untuk keluar\n")

    while True:

        pertanyaan = input("Anda: ").strip()

        if not pertanyaan:
            continue

        if pertanyaan.lower() in ("exit", "quit"):
            print("Sampai jumpa!")
            break

        try:

            # ── 1. Trigger laporan ──
            if need_report(pertanyaan):
                handle_laporan(pertanyaan)
                continue

            # ── 2. Generate SQL ──
            prompt_sql = build_sql_prompt(pertanyaan)
            if DEBUG:
                print(f"PROMPT LENGTH = {len(prompt_sql)}")

            t0      = datetime.now()
            raw_sql = ask_ai(prompt_sql, mode="sql")
            elapsed = (datetime.now() - t0).total_seconds()
            if DEBUG:
                print(f"RESPON AI {elapsed:.1f} detik")
                print(f"\nRAW SQL:\n{raw_sql}\n")






            sql = clean_sql(raw_sql)
            if DEBUG:
                print(f"\nSQL:\n{sql}\n")

            # ── 3. Guard SQL ──
            error = guard_sql(sql)
            if error:
                print(f"⚠ {error}\n")
                continue

            # ── 3b. Validate logic ──
            error = validate_sql(sql, pertanyaan)
            if error:
                print(f"{error}\n")
                continue

            # ── 4. Execute SQL ──
            if DEBUG:
                print("EXECUTING SQL...")
            hasil = run_query(sql)
            if DEBUG:
                print("QUERY SELESAI")

            if hasil.empty:
                print("\nTidak ada data.\n")
                continue

            hasil_str = format_hasil(hasil)
            if DEBUG:
                print(f"\nHASIL:")
            print(f"{hasil_str}\n")

            # ── Export Excel kalau diminta ──
            if need_export(pertanyaan):
                try:
                    filepath = export_excel(hasil, pertanyaan)
                    print(f"✅ Excel tersimpan: {filepath}\n")
                except Exception as ex:
                    print(f"⚠ Gagal export Excel: {ex}\n")

            # ── 5. Analisa (kalau perlu) ──
            if need_analysis(pertanyaan):
                if DEBUG:
                    print("MEMANGGIL ANALYSIS MODEL")
                t0      = datetime.now()
                analisa = ask_ai(
                    build_analyst_prompt(pertanyaan, hasil_str),
                    mode="analysis"
                )
                if DEBUG:
                    elapsed = (datetime.now() - t0).seconds
                    print(f"RESPON AI {elapsed} detik")
                print(f"\nANALISA:\n{analisa}\n")

        except Exception as e:
            print(f"\nERROR: {e}\n")


if __name__ == "__main__":
    main()