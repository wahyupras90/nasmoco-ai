"""
app.py
======
Web interface untuk Nasmoco AI Agent.
Jalankan: python app.py
Akses: http://[IP-PC]:8000 dari HP/laptop manapun di LAN

Mendukung 3 user bersamaan — tiap session punya _last_result sendiri.
"""

import re
import os
import sys
import uuid
import asyncio
import io
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


# ════════════════════════════════════════
# LOGGING — log internal server (lewat stderr, tidak pernah ke-capture)
# ════════════════════════════════════════

def log_server(*args, **kwargs):
    """
    Log internal server — selalu ke stderr, BUKAN stdout.
    redirect_stdout (dipakai capture_run) hanya menangkap stdout, jadi log
    lewat stderr dijamin tidak akan pernah bocor ke output yang dikirim
    ke browser, apapun kondisi race antar request paralel.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]", *args, file=sys.stderr, **kwargs)


# ════════════════════════════════════════
# ROUTING KEYWORDS
# Analisa/investigasi TIDAK ditangani app.py — hanya via main.py (terminal)
# Claude fast ('claude, ...') tetap jalan, dideteksi sql_agent.run() sendiri
# ════════════════════════════════════════

LAPORAN_KEYWORDS = [
    "buat laporan", "generate laporan",
    "laporan bulan", "buat report",
    "generate report", "cetak laporan"
]

EXCEL_KEYWORDS = [
    "export excel", "export ke excel", "simpan excel",
    "simpan ke excel", "download excel", "ke excel",
    "export", "unduh excel", "unduh"
]

SAWA_KEYWORDS = [
    "extended warranty", "sawa",
    "download warranty", "cek warranty",
    "warranty sawa", "ambil warranty",
    "ambil sawa", "download sawa",
    "ambil extended", "sertifikat warranty",
    "sertifikat sawa"
]

VIN_REGEX = r"\b[A-HJ-NPR-Z0-9]{17}\b"

def need_report(text: str) -> bool:
    return any(w in text.lower() for w in LAPORAN_KEYWORDS)

def need_sawa(text: str) -> bool:
    return any(w in text.lower() for w in SAWA_KEYWORDS)

def need_export(text: str) -> bool:
    return any(w in text.lower() for w in EXCEL_KEYWORDS)

def extract_vin_from_text(text: str) -> list:
    """Ekstrak nomor rangka (17 digit VIN) langsung dari teks pertanyaan."""
    return re.findall(VIN_REGEX, text.upper())


# ════════════════════════════════════════
# SESSION STORE — _last_result per user
# ════════════════════════════════════════

import pandas as pd

_sessions: dict[str, pd.DataFrame] = {}        # session_id -> last DataFrame
_sessions_source: dict[str, str] = {}          # session_id -> 'sql_agent' | 'attack_list_pending' | 'attack_list_expired'

def get_last_result(session_id: str) -> pd.DataFrame:
    return _sessions.get(session_id, pd.DataFrame())

def set_last_result(session_id: str, df: pd.DataFrame, source: str = "sql_agent"):
    _sessions[session_id] = df.copy()
    _sessions_source[session_id] = source

def get_last_result_source(session_id: str) -> str:
    return _sessions_source.get(session_id, "sql_agent")


# ════════════════════════════════════════
# SAWA PROGRESS STORE — untuk polling dari browser
# ════════════════════════════════════════

_sawa_progress: dict = {}  # session_id -> {"current", "total", "vin", "status", "done"}

def update_sawa_progress(session_id: str, current: int, total: int, vin: str, status: str):
    _sawa_progress[session_id] = {
        "current": current, "total": total, "vin": vin,
        "status": status, "done": current >= total and status != "processing",
    }

def get_sawa_progress(session_id: str) -> dict:
    return _sawa_progress.get(session_id, {"current": 0, "total": 0, "vin": "", "status": "", "done": True})

def reset_sawa_progress(session_id: str, total: int):
    _sawa_progress[session_id] = {"current": 0, "total": total, "vin": "", "status": "starting", "done": False}

def finish_sawa_progress(session_id: str):
    if session_id in _sawa_progress:
        _sawa_progress[session_id]["done"] = True


# ════════════════════════════════════════
# EXCEL IN-MEMORY — generate bytes tanpa tulis ke disk
# ════════════════════════════════════════

def build_excel_bytes(df: pd.DataFrame) -> bytes:
    """Konversi DataFrame ke bytes Excel siap download."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Data', index=False)
        ws = writer.sheets['Data']
        for col in ws.columns:
            max_len = max(
                len(str(cell.value)) if cell.value else 0
                for cell in col
            )
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)
    buf.seek(0)
    return buf.read()


# ════════════════════════════════════════
# CAPTURE STDOUT — karena run() pakai print()
# ════════════════════════════════════════

def _strip_model_lines(text: str) -> str:
    """
    Hapus baris 'MODEL=...' dari output, termasuk kalau ada prefix timestamp
    menempel di depannya (terjadi kalau modul yang dipanggil sempat meng-cache
    referensi print sebelum builtins.print di-restore ke versi asli).
    """
    lines = [l for l in text.splitlines() if 'MODEL=' not in l]
    return '\n'.join(lines).strip()


def capture_run(fn, *args, **kwargs) -> str:
    """
    Jalankan fn dan kembalikan semua output print() sebagai string.
    Murni redirect_stdout — tidak menyentuh builtins.print sama sekali,
    sehingga aman untuk request paralel (tidak ada shared global state).
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)

    output = buf.getvalue().strip()

    # Sembunyikan baris MODEL= kalau bukan debug mode
    debug = kwargs.get('debug', False)
    if not debug:
        output = _strip_model_lines(output)

    return output


# ════════════════════════════════════════
# PATCH SQL_AGENT — inject _last_result per session
# ════════════════════════════════════════

def run_sql_agent(pertanyaan: str, session_id: str, debug: bool = False) -> str:
    """
    Panggil sql_agent.run() dengan _last_result milik session ini.
    Setelah selesai, ambil _last_result yang baru dan simpan ke session.
    """
    from ai import sql_agent

    # Inject last result milik session ini ke module-level sql_agent
    df_session = get_last_result(session_id)
    sql_agent._last_result = df_session

    # Log untuk verifikasi inject (selalu tampil di terminal server)
    log_server(f"[SESSION {session_id[:8]}] inject _last_result: "
               f"{len(df_session)} baris, kolom={list(df_session.columns)}")

    output = capture_run(sql_agent.run, pertanyaan, debug=debug)

    # Filter MODEL= juga di path sql_agent (capture_run sudah handle, tapi jaga-jaga)
    if not debug:
        output = _strip_model_lines(output)

    # Ambil kembali _last_result yang mungkin sudah diupdate
    new_df = sql_agent._last_result
    set_last_result(session_id, new_df)
    log_server(f"[SESSION {session_id[:8]}] simpan _last_result: "
               f"{len(new_df)} baris, kolom={list(new_df.columns)}")

    return output


# ════════════════════════════════════════
# HANDLER LAPORAN
# ════════════════════════════════════════

def handle_laporan(pertanyaan: str) -> dict:
    """
    Generate laporan HTML, return dict berisi filepath untuk di-stream ke browser.
    """
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

        filepath = buat_laporan(tahun, bulan)
        return {"laporan": True, "filepath": str(filepath)}

    except ImportError:
        return {"error": "⚠ report_generator.py tidak ditemukan di tools/"}
    except Exception as e:
        return {"error": f"ERROR generate laporan: {e}"}


# ════════════════════════════════════════
# ROUTER UTAMA
# ════════════════════════════════════════

async def process_message(pertanyaan: str, session_id: str, debug: bool = False) -> str | dict:
    """
    Router utama. Return:
    - str  → tampilkan sebagai pesan chat biasa
    - dict → {"export": True, "filename": ..., "rows": N} → trigger download di client
    """
    loop = asyncio.get_event_loop()

    try:
        # ── Export Excel — intercept sebelum routing lain ──
        if need_export(pertanyaan):
            df = get_last_result(session_id)
            if df.empty:
                return "⚠ Belum ada hasil query untuk di-export.\nTanya sesuatu dulu, baru ketik 'export excel'."
            return {
                "export": True,
                "filename": f"nasmoco_{datetime.now().strftime('%Y-%m-%d_%H%M')}.xlsx",
                "rows": len(df),
            }

        # ── SAWA / Extended Warranty ──
        if need_sawa(pertanyaan):
            # Coba ambil VIN dari teks pertanyaan langsung
            vin_list = extract_vin_from_text(pertanyaan)
            from_text = bool(vin_list)

            # Kalau tidak ada VIN di teks, ambil dari _last_result session
            if not vin_list:
                df = get_last_result(session_id)
                if 'no_rangka' in df.columns and not df.empty:
                    vin_list = df['no_rangka'].dropna().tolist()

                    # Unit attack list (pending/expired) belum pernah servis SBE 60K,
                    # jadi belum mungkin punya sertifikat SAWA — beri penjelasan, jangan proses
                    source = get_last_result_source(session_id)
                    if source.startswith("attack_list"):
                        return ("⚠ SAWA tidak bisa diambil dari hasil attack list.\n\n"
                                "Sertifikat SAWA baru terbit setelah unit menyelesaikan "
                                "servis SBE 60K. Unit di attack list belum pernah datang "
                                "servis, jadi belum mungkin punya SAWA.\n\n"
                                "Jalankan query unit yang sudah selesai SBE 60K dulu, "
                                "baru ketik 'ambil sawa nya'.")

            if not vin_list:
                return ("⚠ Tidak ada nomor rangka ditemukan.\n"
                        "Ketik nomor rangka langsung atau jalankan query dulu.")

            reset_sawa_progress(session_id, len(vin_list))

            def _on_progress(current, total, vin, status):
                update_sawa_progress(session_id, current, total, vin, status)

            def _run_sawa():
                from tools.extended_warranty import run as run_sawa
                run_sawa(vin_list, auto_confirm=True, on_progress=_on_progress)

            try:
                await loop.run_in_executor(None, lambda: capture_run(_run_sawa))
            finally:
                finish_sawa_progress(session_id)

            # Kirim sinyal download ZIP ke browser
            vins_param = ",".join(vin_list)
            return {
                "sawa_zip": True,
                "vins": vins_param,
                "count": len(vin_list),
            }

        # ── Attack list TCARE (query baku, tidak pakai LLM) ──
        try:
            from tools.attack_list_tcare import (
                is_attack_list_query, is_expired_query,
                get_attack_list, get_attack_list_expired,
                parse_bulan_dari_teks, parse_sa_dari_teks,
            )
            if is_attack_list_query(pertanyaan):
                bulan = parse_bulan_dari_teks(pertanyaan)
                sa    = parse_sa_dari_teks(pertanyaan)
                expired_mode = is_expired_query(pertanyaan)
                fn          = get_attack_list_expired if expired_mode else get_attack_list
                label_mode  = "EXPIRED" if expired_mode else "PENDING"
                source_tag  = "attack_list_expired" if expired_mode else "attack_list_pending"
                count_only  = any(k in pertanyaan.lower()
                                   for k in ['berapa', 'jumlah', 'total', 'ada berapa'])
                bulan_label = bulan or datetime.now().strftime('%Y-%m')

                if count_only:
                    counts = await loop.run_in_executor(
                        None, lambda: fn(bulan=bulan, sa=sa, count_only=True)
                    )
                    label = f" SA {sa}" if sa else ""
                    if expired_mode or counts['total_unit'] == counts['total_pekerjaan']:
                        return (f"Attack list TCARE {label_mode} {bulan_label}{label}: "
                                f"{counts['total_unit']} unit")
                    return (f"Attack list TCARE {label_mode} {bulan_label}{label}: "
                            f"{counts['total_unit']} unit ({counts['total_pekerjaan']} pekerjaan)")

                df = await loop.run_in_executor(
                    None, lambda: fn(bulan=bulan, sa=sa)
                )
                # Tag source: unit attack list belum pernah datang servis,
                # jadi belum mungkin punya SAWA — dicek di SAWA handler nanti
                set_last_result(session_id, df, source=source_tag)

                if df.empty:
                    return (f"Tidak ada attack list TCARE {label_mode} untuk {bulan_label}" +
                            (f" SA {sa}" if sa else "") + ".")

                label = f" SA {sa}" if sa else ""
                return (f"Attack list TCARE {label_mode} {bulan_label}{label} "
                        f"- {len(df)} unit:")
        except ImportError:
            pass  # modul belum ada di environment ini, lanjut ke routing lain

        if need_report(pertanyaan):
            result = await loop.run_in_executor(None, handle_laporan, pertanyaan)
            return result  # dict {"laporan": True, "filepath": ...} atau {"error": ...}

        else:
            # Qwen (default) dan Claude fast ('claude, ...') sama-sama lewat sql_agent
            # sql_agent.run() sendiri yang deteksi prefix 'claude,'
            # Catatan: mode analisa/investigasi TIDAK tersedia di app.py — hanya via main.py
            result = await loop.run_in_executor(
                None, run_sql_agent, pertanyaan, session_id, debug
            )

        return result or "(Tidak ada output)"

    except Exception as e:
        return f"ERROR: {e}"


# ════════════════════════════════════════
# FASTAPI APP
# ════════════════════════════════════════

app = FastAPI(title="Nasmoco AI", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=CHAT_HTML)


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    pertanyaan = body.get("message", "").strip()
    session_id = body.get("session_id", "default")
    debug      = body.get("debug", False)

    if not pertanyaan:
        return JSONResponse({"response": "Pertanyaan kosong."})

    result = await process_message(pertanyaan, session_id, debug)

    # Export Excel mode
    if isinstance(result, dict) and result.get("export"):
        return JSONResponse({
            "export": True,
            "filename": result["filename"],
            "rows": result["rows"],
            "session_id": session_id,
        })

    # Laporan HTML mode
    if isinstance(result, dict) and result.get("laporan"):
        return JSONResponse({
            "laporan": True,
            "filepath": result["filepath"],
        })

    # Error dari handle_laporan
    if isinstance(result, dict) and result.get("error"):
        return JSONResponse({"response": result["error"]})

    # SAWA ZIP mode
    if isinstance(result, dict) and result.get("sawa_zip"):
        return JSONResponse({
            "sawa_zip": True,
            "vins": result["vins"],
            "count": result["count"],
        })

    # Sertakan table_data kalau ada _last_result untuk session ini
    # DAN response tidak menunjukkan "tidak ada data" — supaya tabel lama
    # (dari query sebelumnya) tidak ikut tampil saat query terbaru kosong
    df = get_last_result(session_id)
    table_data = None
    response_text = (result or "").lower()
    no_data_signal = any(s in response_text for s in [
        "tidak ada data", "no data", "kosong", "tidak ditemukan"
    ])

    if not df.empty and len(df) > 1 and not no_data_signal:
        # Kirim sebagai list of dicts, max 500 baris untuk performa
        table_data = {
            "columns": list(df.columns),
            "rows": df.head(500).fillna("").astype(str).values.tolist()
        }

    return JSONResponse({
        "response": result or "(Tidak ada output)",
        "table_data": table_data,
    })


@app.get("/sawa-progress")
async def sawa_progress(session_id: str):
    """Polling endpoint — browser tanya progress download SAWA tiap beberapa detik."""
    return JSONResponse(get_sawa_progress(session_id))


@app.get("/download-excel")
async def download_excel(session_id: str, filename: str = "nasmoco.xlsx"):
    """Endpoint download Excel — ambil _last_result session dan stream ke browser."""
    df = get_last_result(session_id)
    if df.empty:
        return JSONResponse({"error": "Tidak ada data"}, status_code=404)

    excel_bytes = build_excel_bytes(df)

    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download-sawa-zip")
async def download_sawa_zip(vins: str = ""):
    """
    Zip semua PDF SAWA yang sudah didownload dan stream ke browser.
    vins: comma-separated list VIN untuk filter (kosong = semua PDF di folder)
    """
    from pathlib import Path

    PDF_FOLDER = Path(r"D:\AI_nasmoco\Output\Sawa\PDF")
    if not PDF_FOLDER.exists():
        return JSONResponse({"error": "Folder PDF tidak ditemukan"}, status_code=404)

    # Filter berdasarkan VIN kalau ada
    vin_list = [v.strip() for v in vins.split(",") if v.strip()] if vins else []

    if vin_list:
        pdf_files = []
        for vin in vin_list:
            matches = list(PDF_FOLDER.glob(f"{vin}*.pdf"))
            pdf_files.extend(matches)
    else:
        pdf_files = list(PDF_FOLDER.glob("*.pdf"))

    if not pdf_files:
        return JSONResponse({"error": "Tidak ada PDF ditemukan"}, status_code=404)

    # Buat ZIP in-memory
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pdf_path in pdf_files:
            zf.write(pdf_path, pdf_path.name)
    zip_buf.seek(0)

    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"sawa_{ts}.zip"

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download-laporan")
async def download_laporan(filepath: str):
    """
    Stream file laporan HTML dari server ke browser sebagai download.
    filepath dikirim oleh handle_laporan(), divalidasi harus di dalam folder Output.
    """
    from pathlib import Path

    OUTPUT_ROOT = Path(r"D:\AI_nasmoco\Output").resolve()
    target = Path(filepath).resolve()

    # Validasi: file harus ada di dalam folder Output (cegah path traversal)
    try:
        target.relative_to(OUTPUT_ROOT)
    except ValueError:
        return JSONResponse({"error": "Path tidak valid"}, status_code=403)

    if not target.exists():
        return JSONResponse({"error": "File laporan tidak ditemukan"}, status_code=404)

    content = target.read_bytes()

    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


# ════════════════════════════════════════
# CHAT UI HTML — load dari file terpisah
# ════════════════════════════════════════

_HTML_FILE = Path(__file__).parent / "templates" / "chat.html"
CHAT_HTML = _HTML_FILE.read_text(encoding="utf-8")


# ════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════

if __name__ == "__main__":
    import socket

    # Tampilkan IP lokal supaya mudah dibagikan ke tim
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)

    print("\n" + "═" * 42)
    print("  Nasmoco AI — Web Interface")
    print("═" * 42)
    print(f"  Lokal  : http://localhost:8000")
    print(f"  LAN    : http://{local_ip}:8000")
    print("  Akses dari HP: scan QR atau ketik URL di atas")
    print("  Stop   : Ctrl+C")
    print("═" * 42 + "\n")

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )