"""
ai/investigator.py
==================
Mode investigasi Claude — agentic loop dengan human-in-the-loop.
User konfirmasi sebelum setiap query dieksekusi.
"""

import re
from pathlib import Path
from datetime import datetime
from ai.openrouter_client import ask_ai
from db.query import run_query
from ai.sql_agent import clean_sql, guard_sql, format_hasil, detect_pivot


# ════════════════════════════════════════
# LOAD PROMPTS
# ════════════════════════════════════════

_BASE = Path(__file__).parent.parent
SQL_PROMPT    = (_BASE / "prompts/sql_prompt.txt").read_text(encoding="utf-8")
SYSTEM_PROMPT = (_BASE / "prompts/system_prompt.txt").read_text(encoding="utf-8")


# ════════════════════════════════════════
# SIGNAL DETECTION
# ════════════════════════════════════════

STOP_SIGNALS = [
    "cukup", "sudah", "simpulkan", "stop",
    "selesai", "kesimpulan", "done"
]

SKIP_SIGNALS = ["skip", "s", "tidak", "no", "lewat"]

def is_stop(text: str) -> bool:
    return any(s in text.lower() for s in STOP_SIGNALS)

def is_skip(text: str) -> bool:
    return text.lower().strip() in SKIP_SIGNALS

def has_sql(text: str) -> bool:
    return bool(re.search(r'\b(SELECT|WITH)\b', text.upper()))

def extract_sql(text: str) -> str | None:
    match = re.search(r"```sql\s*([\s\S]+?)```", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match2 = re.search(r"((?:WITH|SELECT)\b[\s\S]+?;)", text, re.IGNORECASE)
    if match2:
        return match2.group(1).strip()
    return None


# ════════════════════════════════════════
# SESSION CONTEXT
# ════════════════════════════════════════

def build_ringkasan(session: list) -> str:
    if not session:
        return ""
    lines = ["=== RIWAYAT INVESTIGASI ==="]
    for i, item in enumerate(session, 1):
        lines.append(f"\n[Query {i}] {item['query_desc']}")
        lines.append(f"Hasil ringkas: {item['ringkasan']}")
    return "\n".join(lines)


def buat_ringkasan_hasil(df_str: str, max_rows: int = 5) -> str:
    rows = df_str.strip().splitlines()
    if len(rows) <= max_rows + 1:
        return df_str
    header  = rows[0]
    sample  = rows[1:max_rows+1]
    total   = len(rows) - 1
    ringkas = "\n".join([header] + sample)
    ringkas += f"\n... (total {total} baris)"
    return ringkas


# ════════════════════════════════════════
# INVESTIGATOR PROMPT
# ════════════════════════════════════════

INVESTIGATOR_SYSTEM = f"""{SYSTEM_PROMPT}

════════════════════════════════
MODE INVESTIGASI
════════════════════════════════

Kamu sedang dalam mode investigasi.
Kamu punya akses ke database nasmoco.db.

Untuk mengambil data:
Tulis SQL query dalam blok ```sql ... ```
User akan konfirmasi dulu sebelum query dieksekusi.

PENTING:
- Tulis MAKSIMAL 1 SQL query per respons
- Jelaskan secara singkat TUJUAN query tersebut
- Tunggu hasil sebelum query berikutnya
- Jangan menulis banyak query sekaligus

Setelah melihat data:
- Analisa hasilnya
- Kalau butuh data tambahan, jelaskan apa yang dibutuhkan
  dan tulis 1 SQL query
- Kalau sudah cukup, tulis KESIMPULAN

Jika user meminta list, daftar, tampilkan, show, export:
- Buat 1 query langsung
- JANGAN tulis analisa, temuan, atau KESIMPULAN
- JANGAN tulis breakdown atau statistik
- Tampilkan data saja, tunggu instruksi user

Gunakan KESIMPULAN HANYA jika user eksplisit meminta:
analisa, kenapa, penyebab, evaluasi, rekomendasi

Jika data yang diminta ada di lebih dari satu tabel:
Langsung gunakan JOIN dalam 1 query.
JANGAN query tabel satu per satu.

Aturan SQL yang harus diikuti:
{SQL_PROMPT}
"""


def build_investigator_prompt(pertanyaan: str,
                               ringkasan_session: str,
                               hasil_terbaru: str = "",
                               user_input: str = "") -> str:
    parts = [f"Pertanyaan investigasi:\n{pertanyaan}"]
    if ringkasan_session:
        parts.append(ringkasan_session)
    if hasil_terbaru:
        parts.append(f"=== HASIL QUERY TERBARU ===\n{hasil_terbaru}")
    if user_input:
        parts.append(f"=== INPUT USER ===\n{user_input}")
    parts.append(
        "Lanjutkan investigasi. Jika butuh data tambahan, "
        "tulis 1 SQL dalam ```sql...```. "
        "Jika sudah cukup, tulis KESIMPULAN."
    )
    return "\n\n".join(parts)


# ════════════════════════════════════════
# CACHED MESSAGES BUILDER
# ════════════════════════════════════════

def _build_investigator_messages(user_prompt: str) -> list:
    """
    Bangun messages array dengan cache_control pada INVESTIGATOR_SYSTEM (statis).
    User prompt (dinamis) dikirim tanpa cache.
    """
    return [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": INVESTIGATOR_SYSTEM,
                    "cache_control": {"type": "ephemeral"}
                }
            ]
        },
        {
            "role": "user",
            "content": user_prompt
        }
    ]


# ════════════════════════════════════════
# AUTO MODE — untuk app.py/web (tanpa input())
# ════════════════════════════════════════

def run_auto(pertanyaan: str, debug: bool = False) -> str:
    """
    Mode otomatis untuk browser — 1 call langsung ke Claude, langsung jawab.
    Tidak ada loop, tidak ada input(), tidak ada konfirmasi.
    """
    from datetime import datetime

    prompt = build_investigator_prompt(
        pertanyaan        = pertanyaan,
        ringkasan_session = "",
        hasil_terbaru     = "",
        user_input        = "Jawab langsung dan tulis KESIMPULAN. Jangan minta konfirmasi.",
    )

    t0       = datetime.now()
    response = ask_ai(
        user_prompt = _build_investigator_messages(prompt),
        mode        = "analysis"
    )
    elapsed = (datetime.now() - t0).total_seconds()

    if debug:
        print(f"[run_auto] RESPON CLAUDE {elapsed:.1f} detik")

    return response


# ════════════════════════════════════════
# RUN — entry point dari main.py
# ════════════════════════════════════════

def run(pertanyaan: str, debug: bool = False):
    print(f"SQL_PROMPT length: {len(SQL_PROMPT)}")
    print(f"SYSTEM_PROMPT length: {len(SYSTEM_PROMPT)}")
    print(f"INVESTIGATOR_SYSTEM length: {len(INVESTIGATOR_SYSTEM)}")
    print("\n🔍 MODE INVESTIGASI CLAUDE")
    print("Konfirmasi setiap query sebelum dieksekusi.")
    print("Ketik 'cukup' untuk mengakhiri.\n")

    session       = []
    hasil_terbaru = ""
    user_input    = ""
    turn          = 0

    while True:
        turn += 1

        # ── Build + kirim ke Claude ──
        ringkasan = build_ringkasan(session)
        prompt    = build_investigator_prompt(
            pertanyaan        = pertanyaan,
            ringkasan_session = ringkasan,
            hasil_terbaru     = hasil_terbaru,
            user_input        = user_input,
        )

        if debug:
            print(f"[Turn {turn}] PROMPT LENGTH = {len(prompt)}")

        t0       = datetime.now()
        response = ask_ai(
            user_prompt = _build_investigator_messages(prompt),
            mode        = "analysis"
        )
        elapsed = (datetime.now() - t0).total_seconds()

        if debug:
            print(f"[Turn {turn}] RESPON CLAUDE {elapsed:.1f} detik")

        # Sembunyikan SQL dari tampilan user (kecuali debug mode)
        if debug:
            print(f"\nCLAUDE:\n{response}\n")
        else:
            response_display = re.sub(
                r'```sql[\s\S]+?```', '[SQL disiapkan]', response, flags=re.IGNORECASE
            ).strip()
            print(f"\nCLAUDE:\n{response_display}\n")

        # ── Cek apakah ada SQL ──
        if has_sql(response):
            sql_raw = extract_sql(response)
            if sql_raw:
                sql = clean_sql(sql_raw)

                if debug:
                    print(f"SQL:\n{sql}\n")

                # ── KONFIRMASI USER SEBELUM EXECUTE ──
                print("─" * 40)
                konfirmasi = input(
                    "Jalankan query? (ya/tidak/cukup) "
                    "atau tulis arahan: "
                ).strip()
                print("─" * 40)

                k_lower = konfirmasi.lower()

                # Stop → minta kesimpulan
                if is_stop(k_lower):
                    if not session:
                        print("\n⚠ Belum ada data yang dikumpulkan.")
                        print("Jalankan minimal 1 query dulu sebelum simpulkan.\n")
                        user_input = "User meminta kesimpulan tapi belum ada query yang dieksekusi. Jelaskan bahwa kamu butuh data dulu."
                        hasil_terbaru = ""
                        continue
                    print("\nMembuat kesimpulan...\n")
                    final_prompt = build_investigator_prompt(
                        pertanyaan        = pertanyaan,
                        ringkasan_session = build_ringkasan(session),
                        user_input        = "Tulis KESIMPULAN FINAL berdasarkan semua data di atas."
                    )
                    kesimpulan = ask_ai(
                        user_prompt = _build_investigator_messages(final_prompt),
                        mode        = "analysis"
                    )
                    print(f"\nKESIMPULAN:\n{kesimpulan}\n")
                    break

                # Skip tanpa arahan → minta arahan
                if is_skip(k_lower):
                    arahan = input("Arahan untuk Claude: ").strip()
                    if not arahan:
                        arahan = "Query di-skip. Ajukan query yang berbeda."
                    user_input    = f"Query di-skip. Arahan user: {arahan}"
                    hasil_terbaru = ""
                    continue

                # Arahan langsung (bukan ya/skip/cukup) → skip + kirim arahan
                if k_lower not in ("ya", "y", "yes", "iya"):
                    user_input    = f"Query di-skip. Arahan user: {konfirmasi}"
                    hasil_terbaru = ""
                    continue

                # Execute
                error = guard_sql(sql)
                if error:
                    print(f"⚠ {error}")
                    hasil_terbaru = f"ERROR: {error}"
                    user_input    = ""
                    continue

                try:
                    hasil = run_query(sql)

                    if hasil.empty:
                        hasil_terbaru = "Query berhasil tapi tidak ada data."
                        print("Tidak ada data.\n")
                    else:
                        hasil_str     = format_hasil(hasil)
                        hasil_terbaru = hasil_str
                        print(f"HASIL QUERY:\n{hasil_str}\n")
                        session.append({
                            "query_desc": sql[:80] + "..." if len(sql) > 80 else sql,
                            "ringkasan":  buat_ringkasan_hasil(hasil_str),
                        })

                except Exception as e:
                    hasil_terbaru = f"ERROR eksekusi: {e}"
                    print(f"⚠ {hasil_terbaru}\n")

                user_input = ""
                continue

        # ── Tidak ada SQL — tunggu input user ──
        hasil_terbaru = ""

        if "KESIMPULAN" in response.upper():
            if not session:
                print("\n⚠ Claude membuat kesimpulan tanpa data.")
                print("Minta Claude jalankan query dulu.\n")
                user_input    = "JANGAN membuat kesimpulan tanpa data. Kamu belum menjalankan query apapun. Ajukan query pertama dulu."
                hasil_terbaru = ""
                continue
            print("\n✅ Investigasi selesai.\n")
            break

        user_input = input("Anda: ").strip()
        if not user_input:
            continue

        if is_stop(user_input):
            if not session:
                print("\n⚠ Belum ada data yang dikumpulkan.")
                print("Jalankan minimal 1 query dulu sebelum simpulkan.\n")
                user_input = "User meminta kesimpulan tapi belum ada query yang dieksekusi. Jelaskan bahwa kamu butuh data dulu."
                continue
            print("\nMembuat kesimpulan...\n")
            final_prompt = build_investigator_prompt(
                pertanyaan        = pertanyaan,
                ringkasan_session = build_ringkasan(session),
                user_input        = "Tulis KESIMPULAN FINAL berdasarkan semua data di atas."
            )
            kesimpulan = ask_ai(
                user_prompt = _build_investigator_messages(final_prompt),
                mode        = "analysis"
            )
            print(f"\nKESIMPULAN:\n{kesimpulan}\n")
            break