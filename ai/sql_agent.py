"""
ai/sql_agent.py
===============
Flow SQL: pertanyaan → SQL (Qwen/DeepSeek) → execute → analisa (Claude).
Semua logic SQL ada di sini — main.py hanya memanggil run().
"""

import re
import os
import pandas as pd
from pathlib import Path
from datetime import datetime
from ai.openrouter_client import ask_ai
from db.query import run_query


# ════════════════════════════════════════
# LOAD PROMPTS
# ════════════════════════════════════════

SQL_PROMPT    = Path("prompts/sql_prompt.txt").read_text(encoding="utf-8")
SYSTEM_PROMPT = Path("prompts/system_prompt.txt").read_text(encoding="utf-8")

# RAG builder untuk Qwen — pilih chunk sesuai pertanyaan
try:
    from ai.rag_builder import build_prompt as rag_build_prompt
    USE_RAG = True
except ImportError:
    USE_RAG = False


# ════════════════════════════════════════
# ROUTING KEYWORDS
# ════════════════════════════════════════

# Hasil query terakhir — untuk referensi query berikutnya
_last_result: pd.DataFrame = pd.DataFrame()

ANALYSIS_KEYWORDS = [
    "analisa", "analysis", "kenapa", "mengapa",
    "penyebab", "root cause", "insight",
    "rekomendasi", "warning", "bandingkan",
    "compare", "trend", "growth", "evaluasi"
]

EXCEL_KEYWORDS = [
    "export excel", "export ke excel", "simpan excel",
    "simpan ke excel", "download excel", "ke excel",
    "export", "unduh excel"
]

SAWA_KEYWORDS = [
    "extended warranty", "sawa",
    "download warranty", "cek warranty",
    "warranty sawa", "ambil warranty",
    "ambil sawa", "download sawa",
    "ambil extended", "sertifikat warranty",
    "sertifikat sawa"
]

def need_sawa(text: str) -> bool:
    return any(w in text.lower() for w in SAWA_KEYWORDS)

OUTLET_KEYWORDS = [
    "outlet", "dealer", "nasmoco",
    "bengkel", "workshop", "cabang",
    "total outlet", "total bengkel",
]

def need_analysis(text: str) -> bool:
    return any(w in text.lower() for w in ANALYSIS_KEYWORDS)

def need_export(text: str) -> bool:
    return any(w in text.lower() for w in EXCEL_KEYWORDS)

DIRECT_EXPORT_KEYWORDS = [
    "export", "simpan excel", "simpan di excel",
    "download excel", "ke excel", "export excel", 
    "unduh", "save excel"
]
def is_direct_export(text: str) -> bool:
    """Pertanyaan hanya minta export tanpa query baru."""
    t = text.lower().strip()
    # Hanya export kalau kalimat pendek dan tidak ada kata query
    query_words = ['list','tampilkan','berapa','siapa','kapan',
                   'tcare','cpus','revenue','sa','bulan','tahun']
    has_query = any(w in t for w in query_words)
    has_export = any(w in t for w in DIRECT_EXPORT_KEYWORDS)
    return has_export and not has_query


# ════════════════════════════════════════
# CLEAN SQL
# ════════════════════════════════════════

FORBIDDEN_PATTERNS = [
    r'\bDROP\s+TABLE\b',
    r'\bDELETE\s+FROM\b',
    r'\bINSERT\s+INTO\b',
    r'\bUPDATE\s+\w+\s+SET\b',
    r'\bALTER\s+TABLE\b',
    r'\bCREATE\s+TABLE\b',
    r'\bREPLACE\s+INTO\b',
    r'\bTRUNCATE\s+TABLE\b',
]

def clean_sql(raw: str) -> str:
    sql = str(raw)
    sql = re.sub(r"```sql", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"```", "", sql)

    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--") or stripped.startswith("#"):
            continue
        lines.append(line)
    sql = "\n".join(lines)

    upper = sql.upper()
    pos   = upper.find("SELECT")
    pos_w = upper.find("WITH")

    if pos_w >= 0 and (pos < 0 or pos_w < pos):
        sql = sql[pos_w:]
    elif pos >= 0:
        sql = sql[pos:]

    sql = sql.strip()
    if sql and not sql.endswith(";"):
        sql += ";"
    return sql


# Typo kolom yang sering terjadi → auto-fix sebelum execute
COLUMN_FIXES = {
    r'\btcara_type\b':   'tcare_type',
    r'\btcare_typ\b':    'tcare_type',
    r'\bbatas_tare\b':   'batas_tcare',
    r'\btotal_rev\b':    'total_revenue',
    r'\bsa_terakhi\b':   'sa_terakhir',
    r'\blast_sbe_k\b':   'last_sbe_km',
    r'\bsisa_servis\b':  'sisa_service',
    r'\bsisa_detal\b':   'sisa_detail',
    r'\bflag_pendin\b':  'flag_pending_sbe',
}

def fix_typos(sql: str) -> str:
    """Auto-fix typo nama kolom yang umum terjadi."""
    for pattern, correct in COLUMN_FIXES.items():
        sql = re.sub(pattern, correct, sql, flags=re.IGNORECASE)
    return sql


def guard_sql(sql: str) -> str | None:
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, sql, re.IGNORECASE):
            return "Query diblokir: mengandung statement berbahaya"
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        return "Query diblokir: harus dimulai dengan SELECT atau WITH"
    return None


def validate_sql(sql: str, pertanyaan: str) -> str | None:
    sql_upper   = sql.upper()
    tanya_lower = pertanyaan.lower()

    # Rule 1: outlet tapi GROUP BY sa
    is_outlet    = any(k in tanya_lower for k in OUTLET_KEYWORDS)
    has_group_sa = bool(re.search(r'GROUP\s+BY[^;]*\bSA\b', sql_upper))
    if is_outlet and has_group_sa:
        return (
            "⚠ Query outlet tidak boleh GROUP BY sa.\n"
            "Gunakan SUM global tanpa GROUP BY."
        )

    # Rule 2: target tapi tidak ada target_bulanan
    is_target = bool(re.search(
        r'(target|pencapaian|achievement|ach|vs\s*target)', tanya_lower
    ))
    if is_target and "TARGET_BULANAN" not in sql_upper:
        return (
            "⚠ Pertanyaan tentang target tapi query tidak JOIN target_bulanan.\n"
            "Coba ulangi pertanyaan dengan lebih spesifik."
        )

    # Rule 3: tcare warning
    is_tcare = any(k in tanya_lower for k in [
        "tcare", "expired", "expiry", "habis tcare", "batas tcare"
    ])
    has_tcare_tables = (bool(re.search(r'\bRS\b', sql_upper)) or
                        "UNITMASUK" in sql_upper or
                        "REKAPBULANAN" in sql_upper or
                        "UNIT_TCARE" in sql_upper or
                        "TCARE_UNIT" in sql_upper)
    if is_tcare and not has_tcare_tables:
        print("⚠ WARNING: pertanyaan TCARE tapi tidak ada tabel yang relevan.")

    return None


# ════════════════════════════════════════
# FORMAT HASIL
# ════════════════════════════════════════

PCT_KEYWORDS = ['pct', 'persen', '%', 'ratio', 'rate']


def detect_pivot(df):
    """
    Deteksi apakah df cocok untuk pivot:
    - Tepat 3 kolom: 1 numerik (nilai), 2 teks (index + pivot)
    - Kolom pivot: unique values <= 20
    """
    if len(df.columns) != 3:
        return None

    num_cols  = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    text_cols = [c for c in df.columns if c not in num_cols]

    if len(num_cols) != 1 or len(text_cols) != 2:
        return None

    val_col = num_cols[0]

    def is_period_col(col):
        name_hint = any(k in col.lower() for k in
            ['bulan','tanggal','tahun','periode','date','month','year'])
        if name_hint:
            return True
        sample = df[col].dropna().astype(str).head(5)
        return all(re.match(r'^\d{4}(-\d{2})?$', v) for v in sample)

    period_cols = [c for c in text_cols if is_period_col(c)]
    if not period_cols:
        c0, c1 = text_cols
        period_cols = [c0] if df[c0].nunique() >= df[c1].nunique() else [c1]

    idx_col   = period_cols[0]
    pivot_col = [c for c in text_cols if c != idx_col][0]

    if df[pivot_col].nunique() > 20:
        return None

    return idx_col, pivot_col, val_col


def format_hasil(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "Tidak ada data"

    df = df.copy()

    # Tampilkan vertikal kalau hanya 1 baris
    if len(df) == 1:
        row = df.iloc[0]
        lines = []
        max_col_len = max(len(str(c)) for c in df.columns)
        for col in df.columns:
            val = row[col]
            if pd.isna(val):
                val = "-"
            elif pd.api.types.is_numeric_dtype(df[col]):
                is_pct = any(k in col.lower() for k in PCT_KEYWORDS)
                val = f"{val:,.1f}" if is_pct else f"{val:,.0f}"
            lines.append(f"{str(col):<{max_col_len}} : {val}")
        return "\n".join(lines)

    # Coba auto pivot kalau ada 3 kolom dengan pola dimensi × kategori × nilai
    pivot_info = detect_pivot(df)
    if pivot_info:
        idx_col, pivot_col, val_col = pivot_info
        try:
            pivoted = df.pivot_table(
                index=idx_col, columns=pivot_col,
                values=val_col, aggfunc='sum'
            ).reset_index()
            pivoted.columns.name = None
            pivoted = pivoted.fillna(0)
            for c in pivoted.columns:
                if c == idx_col:
                    continue
                if pd.api.types.is_numeric_dtype(pivoted[c]):
                    is_pct = any(k in str(c).lower() for k in PCT_KEYWORDS)
                    pivoted[c] = pivoted[c].apply(
                        lambda x: '-' if x == 0
                        else (f"{x:,.1f}" if is_pct else f"{int(x):,}")
                    )
            return pivoted.to_string(index=False)
        except Exception:
            pass  # fallback ke format biasa

    # Format biasa
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            is_pct = any(k in col.lower() for k in PCT_KEYWORDS)
            df[col] = df[col].apply(
                lambda x: (f"{x:,.1f}" if is_pct else f"{x:,.0f}")
                if pd.notna(x) else "-"
            )
    return df.to_string(index=False)



# ════════════════════════════════════════
# EXPORT EXCEL
# ════════════════════════════════════════

def export_excel(df: pd.DataFrame,
                 pertanyaan: str,
                 output_base: str = "Output") -> str:
    keywords = {
        "tcare":"tcare", "revenue":"revenue", "cpus":"cpus",
        "sa":"sa", "ranking":"ranking", "liter":"liter",
        "upselling":"upselling", "target":"target",
        "rush":"model_rush", "avanza":"model_avanza", "agya":"model_agya",
    }
    p      = pertanyaan.lower()
    topik  = next((v for k, v in keywords.items() if k in p), "data")
    tgl    = datetime.now().strftime("%Y-%m-%d")
    folder = Path(output_base) / "export"
    folder.mkdir(parents=True, exist_ok=True)
    filepath = folder / f"{topik}_{tgl}.xlsx"

    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Data', index=False)
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

TCARE_KEYWORDS = ['tcare', 'batas_tcare', 'sbe terakhir',
                  'habis tcare', 'expired tcare', 'sisa tcare']

def build_sql_prompt(pertanyaan: str) -> list:
    """
    RAG mode (Qwen): pilih chunk sesuai pertanyaan.
    Fallback ke sql_prompt.txt penuh kalau rag_builder tidak ada.
    TCARE hint tetap diinjeksi ke user message.
    """
    tcare_hint = ""
    if any(k in pertanyaan.lower() for k in TCARE_KEYWORDS):
        tcare_hint = (
            "⚠ TCARE QUERY: WAJIB gunakan FROM unit_tcare\n"
            "JANGAN buat CTE dari unitmasuk untuk SBE/SA terakhir.\n"
            "Contoh: SELECT no_rangka, sa_terakhir, last_sbe_km "
            "FROM unit_tcare WHERE ...\n\n"
        )

    # Pilih prompt — RAG atau full
    if USE_RAG:
        prompt_text = rag_build_prompt(pertanyaan)
    else:
        prompt_text = SQL_PROMPT

    user_content = (
        f"{tcare_hint}"
        f"Pertanyaan user:\n{pertanyaan}\n\n"
        f"TUGAS:\nBuat 1 query SQL saja."
    )

    return [
        {
            "role": "user",
            "content": prompt_text + "\n\n" + user_content
        }
    ]

def build_analyst_prompt(pertanyaan: str, hasil_str: str) -> str:
    return f"""Pertanyaan:
{pertanyaan}

Hasil data:
{hasil_str}

Buat analisa dengan format:

FAKTA DATA
TEMUAN
HIPOTESIS (jika ada — beri label HIPOTESIS)
REKOMENDASI

Ringkas. Jangan mengarang. Jangan asumsikan perilaku SA.
"""


# ════════════════════════════════════════
# RUN — entry point dari main.py
# ════════════════════════════════════════

def run(pertanyaan: str, debug: bool = False):
    """
    Jalankan flow SQL lengkap:
    pertanyaan → SQL → execute → [analisa] → [export]
    """
    # 0. Direct export — kalau hanya minta export tanpa query baru
    global _last_result
    if is_direct_export(pertanyaan):
        if not _last_result.empty:
            print(f"📊 Export hasil query sebelumnya ({len(_last_result):,} baris)...")
            try:
                path = export_excel(_last_result, pertanyaan)
                print(f"✅ Excel tersimpan: {path}\n")
            except Exception as ex:
                print(f"⚠ Gagal export: {ex}\n")
            return
        else:
            print("⚠ Belum ada hasil query untuk di-export.\n")
            return

    # 0b. Direct SAWA — skip SQL, pakai _last_result langsung
    if need_sawa(pertanyaan) and 'no_rangka' in _last_result.columns and not _last_result.empty:
        try:
            from tools.extended_warranty import run as run_sawa
            no_rangka_list = _last_result['no_rangka'].dropna().tolist()
            print(f"ℹ Menggunakan {len(no_rangka_list)} no_rangka dari query sebelumnya...\n")
            run_sawa(no_rangka_list)
        except ImportError as e:
            print(f"⚠ extended_warranty.py tidak ditemukan: {e}\n")
        return

    # 1. Generate SQL
    messages_sql = build_sql_prompt(pertanyaan)
    if debug:
        # Hitung total panjang konten untuk debug
        total_len = len("".join(str(m.get("content","")) for m in messages_sql))
        label = "RAG" if USE_RAG else "SQL_PROMPT cached"
        print(f"PROMPT LENGTH = {total_len} ({label})")

    t0      = datetime.now()
    raw_sql = ask_ai(messages_sql, mode="sql")
    elapsed = (datetime.now() - t0).total_seconds()
    if debug:
        print(f"RESPON AI {elapsed:.1f} detik")
        print(f"\nRAW SQL:\n{raw_sql}\n")

    sql = clean_sql(raw_sql)
    sql = fix_typos(sql)
    if debug:
        print(f"\nSQL:\n{sql}\n")

    # 2. Guard + Validate
    error = guard_sql(sql)
    if error:
        print(f"⚠ {error}\n")
        return

    error = validate_sql(sql, pertanyaan)
    if error:
        print(f"{error}\n")
        return

    # 3. Execute
    if debug:
        print("EXECUTING SQL...")
    t_sql = datetime.now()
    hasil = run_query(sql)
    elapsed_sql = (datetime.now() - t_sql).total_seconds()
    if debug:
        print(f"SQL EXECUTION = {elapsed_sql:.2f} detik")
        print("QUERY SELESAI")

    if hasil.empty:
        print("\nTidak ada data.\n")
        return

    # Simpan hasil terakhir untuk referensi berikutnya
    _last_result = hasil.copy()

    hasil_str = format_hasil(hasil)
    if debug:
        print("HASIL:")
    print(f"{hasil_str}\n")

    # 4. Export Excel
    if need_export(pertanyaan):
        try:
            path = export_excel(hasil, pertanyaan)
            print(f"✅ Excel tersimpan: {path}\n")
        except Exception as ex:
            print(f"⚠ Gagal export: {ex}\n")

    # 5. Extended Warranty (SAWA)
    if need_sawa(pertanyaan):
        try:
            from tools.extended_warranty import run as run_sawa
            # Coba dari hasil query sekarang
            if 'no_rangka' in hasil.columns:
                no_rangka_list = hasil['no_rangka'].dropna().tolist()
            # Fallback ke hasil query sebelumnya
            elif 'no_rangka' in _last_result.columns:
                print("ℹ Menggunakan hasil query sebelumnya...\n")
                no_rangka_list = _last_result['no_rangka'].dropna().tolist()
            else:
                no_rangka_list = []

            if no_rangka_list:
                run_sawa(no_rangka_list)
            else:
                print("⚠ Tidak ada no_rangka ditemukan di hasil query.\n")
        except ImportError as e:
            print(f"⚠ extended_warranty.py tidak ditemukan: {e}\n")

    # 6. Analisa
    if need_analysis(pertanyaan):
        if debug:
            print("MEMANGGIL ANALYSIS MODEL")
        t0      = datetime.now()
        analisa = ask_ai(
            build_analyst_prompt(pertanyaan, hasil_str),
            system_prompt=SYSTEM_PROMPT,
            mode="analysis"
        )
        if debug:
            elapsed = (datetime.now() - t0).total_seconds()
            print(f"RESPON AI {elapsed:.1f} detik")
        print(f"\nANALISA:\n{analisa}\n")