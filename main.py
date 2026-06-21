"""
main.py
=======
Router tipis — deteksi mode lalu delegasikan ke modul yang tepat.

Mode:
  normal    → sql_agent.run()       Qwen generate SQL (data, ranking, dll)
  analisa   → investigator.run()    Claude investigasi langsung
  claude    → investigator.run()    Claude agentic loop (manual trigger)
  laporan   → tools/report_generator
"""

import argparse
import os
import re
from pathlib import Path
from datetime import datetime


# ════════════════════════════════════════
# ROUTING KEYWORDS
# ════════════════════════════════════════

LAPORAN_KEYWORDS = [
    "buat laporan", "generate laporan",
    "laporan bulan", "buat report",
    "generate report", "cetak laporan"
]

ANALYSIS_KEYWORDS = [
    "analisa", "analisis", "investigasi",
    "kenapa", "mengapa", "penyebab",
    "bandingkan", "compare", "trend",
    "growth", "evaluasi", "root cause",
]

def need_report(text: str) -> bool:
    return any(w in text.lower() for w in LAPORAN_KEYWORDS)

def need_claude(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("claude ") or t.startswith("claude,")

def need_analysis(text: str) -> bool:
    return any(w in text.lower() for w in ANALYSIS_KEYWORDS)


# ════════════════════════════════════════
# LAPORAN HANDLER
# ════════════════════════════════════════

def handle_laporan(pertanyaan: str):
    try:
        from tools.report_generator import buat_laporan

        bulan_map = {
            'januari':1,'februari':2,'maret':3,'april':4,
            'mei':5,'juni':6,'juli':7,'agustus':8,
            'september':9,'oktober':10,'november':11,'desember':12
        }
        tahun, bulan = None, None
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
# MAIN
# ════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true',
                        help='Tampilkan detail SQL dan proses')
    args  = parser.parse_args()
    DEBUG = args.debug
    os.environ['NASMOCO_DEBUG'] = '1' if DEBUG else '0'

    print("\n" + "═" * 42)
    print("  AI Nasmoco Analyst" + (" [DEBUG]" if DEBUG else ""))
    print("═" * 42)
    print("  normal  : tanya langsung")
    print("  analisa : otomatis → Claude")
    print("  claude  : ketik 'claude [pertanyaan]'")
    print("  keluar  : ketik 'exit'")
    print("═" * 42 + "\n")

    from ai.sql_agent    import run as sql_run
    from ai.investigator import run as investigator_run

    while True:
        pertanyaan = input("Anda: ").strip()

        if not pertanyaan:
            continue

        if pertanyaan.lower() in ("exit", "quit"):
            print("Sampai jumpa!")
            break

        try:
            # ── Mode laporan ──
            if need_report(pertanyaan):
                handle_laporan(pertanyaan)

            # ── Mode investigasi Claude (manual trigger) ──
            elif need_claude(pertanyaan):
                q = re.sub(r'^claude[,\s]+', '', pertanyaan, flags=re.IGNORECASE).strip()
                investigator_run(q, debug=DEBUG)

            # ── Mode analisa → investigator Claude otomatis ──
            elif need_analysis(pertanyaan):
                investigator_run(pertanyaan, debug=DEBUG)

            # ── Mode SQL biasa ──
            else:
                sql_run(pertanyaan, debug=DEBUG)

        except Exception as e:
            print(f"\nERROR: {e}\n")


if __name__ == "__main__":
    main()