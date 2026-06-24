"""
tools/extended_warranty.py
==========================
Download + parse + simpan sertifikat Extended Warranty (SAWA)
dari aftersales.toyota.astra.co.id

Flow:
1. Terima list no_rangka dari AI agent
2. Tampil list + konfirmasi user
3. Download PDF (skip jika sudah ada)
4. Parse PDF → extract data
5. Simpan ke DB tabel extended_warranty + Excel rekap
"""

import re
import os
import sqlite3
import imaplib
import email
import html
import time
import pandas as pd
import pymupdf

from pathlib import Path
from datetime import datetime
from collections import defaultdict
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta

try:
    from config import DB_PATH
except ImportError:
    DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"

# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════

NAMA      = "PT. NEW RATNA MOTOR"
EMAIL_OTP = "wahyu.prasetya@nasmoco.co.id"
APP_PASS  = ""  # isi dari .env atau config

# Global Gmail connection (persistent, login sekali)
_mail         = None
_gmail_ready  = False

PDF_FOLDER   = Path(r"D:\AI_nasmoco\Output\Sawa\PDF")
REKAP_FOLDER = Path(r"D:\AI_nasmoco\Output\Sawa\Rekap")

PDF_FOLDER.mkdir(parents=True, exist_ok=True)
REKAP_FOLDER.mkdir(parents=True, exist_ok=True)

VIN_REGEX     = r"\b[A-HJ-NPR-Z0-9]{17}\b"


# ════════════════════════════════════════
# LOAD CREDENTIALS DARI .ENV
# ════════════════════════════════════════

def _load_env():
    global APP_PASS
    env_path = Path(r"D:\AI_nasmoco\.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith('SAWA_APP_PASS='):
                APP_PASS = line.split('=', 1)[1].strip()


_load_env()


# ════════════════════════════════════════
# GMAIL — persistent connection
# ════════════════════════════════════════

def connect_gmail(force=False):
    global _mail, _gmail_ready
    if _mail is not None and not force:
        return
    try:
        if _mail:
            _mail.logout()
    except Exception:
        pass
    print("  Login Gmail...")
    _mail = imaplib.IMAP4_SSL("mail.nasmoco.co.id")
    _mail.login(EMAIL_OTP, APP_PASS.replace(" ", ""))
    _gmail_ready = True
    print("  Gmail connected")


def get_last_email_id() -> bytes:
    """Snapshot ID email terakhir sebelum trigger OTP."""
    global _mail
    try:
        _mail.select('INBOX')
    except Exception:
        connect_gmail(force=True)
        _mail.select('INBOX')
    _, messages = _mail.search(None, "ALL")
    ids = messages[0].split()
    return ids[-1] if ids else b"0"


def ambil_otp(last_id_awal: bytes) -> str:
    global _mail
    print("  Menunggu OTP...")
    for _ in range(60):
        try:
            _mail.select('INBOX')
        except Exception:
            connect_gmail(force=True)
            _mail.select('INBOX')
        _, messages = _mail.search(None, "ALL")
        ids = messages[0].split()
        if not ids or ids[-1] == last_id_awal:
            time.sleep(2)
            continue

        _, msg_data = _mail.fetch(ids[-1], "(RFC822)")
        msg  = email.message_from_bytes(msg_data[0][1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ["text/plain", "text/html"]:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode(errors="ignore") + " "
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        clean = html.unescape(re.sub(r'<[^>]+>', ' ',
                              re.sub(r'<style.*?>.*?</style>', '', body,
                                     flags=re.IGNORECASE | re.DOTALL)))
        otp = re.search(r"\b\d{6}\b", clean)
        if otp:
            print(f"  OTP: {otp.group()}")
            try:
                _mail.store(ids[-1], '+FLAGS', '\\Deleted')
                _mail.expunge()
                print("  Email OTP dihapus")
            except Exception:
                pass
            return otp.group()
        time.sleep(2)
    raise Exception("OTP timeout")


# ════════════════════════════════════════
# DOWNLOAD PDF
# ════════════════════════════════════════

def _login_otp(page):
    global _gmail_ready
    if not _gmail_ready:
        raise Exception("Butuh OTP tapi Gmail tidak tersedia")

    page.get_by_placeholder("Name").fill(NAMA)
    page.wait_for_timeout(1000)
    page.locator('input[type="email"]').first.fill(EMAIL_OTP)
    page.locator("#react-select-2-input").click()
    page.wait_for_timeout(1000)
    page.locator("#react-select-2-input").fill("NASMOCO - TEGAL")
    page.wait_for_timeout(2000)
    page.keyboard.press("Enter")
    page.locator('input[type="checkbox"]').check()
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="Lanjut").click()
    page.wait_for_timeout(3000)

    # Snapshot email SEBELUM trigger OTP
    last_email_id = get_last_email_id()

    page.get_by_role("button", name="Lanjut").click()
    print("  Halaman OTP")

    otp = ambil_otp(last_email_id)
    for i, digit in enumerate(otp, start=1):
        page.locator(f"#otp{i}").fill(digit)
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="Lanjut").click()
    print("  OTP submitted")
    page.wait_for_timeout(5000)


def download_satu(page, vin: str) -> tuple:
    """Download 1 PDF. Return (status, keterangan, filepath)."""
    file_path = PDF_FOLDER / f"{vin}_{NAMA}.pdf"

    if file_path.exists():
        return "Skipped", "File exists", str(file_path)

    page.goto("https://aftersales.toyota.astra.co.id/t-care")
    page.wait_for_timeout(3000)
    page.get_by_placeholder("Masukkan 17 Digit No. Rangka Kendaraan").first.fill(vin)
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="Lanjut").click()
    page.wait_for_timeout(3000)
    page.get_by_role("button", name="Lanjut").click()
    page.wait_for_timeout(6000)

    try:
        perlu_otp = page.get_by_placeholder("Name").is_visible()
    except Exception:
        perlu_otp = False

    if perlu_otp:
        _login_otp(page)
    else:
        print("  VIN tidak perlu OTP")

    try:
        page.get_by_role("button", name="Warranty").click()
        page.wait_for_timeout(3000)
    except Exception:
        print("  Tab Warranty tidak ada, langsung download")

    with page.expect_download() as dl_info:
        page.locator("button.download_pdf_buttons").first.click()

    dl_info.value.save_as(str(file_path))
    return "Downloaded", "OK", str(file_path)


def download_batch(vin_list: list) -> pd.DataFrame:
    """Download PDF untuk semua VIN dalam list."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("⚠ playwright tidak terinstall. Jalankan: pip install playwright")
        return pd.DataFrame()

    log = []
    with sync_playwright() as p:
        for vin in vin_list:
            print(f"\n  [{vin_list.index(vin)+1}/{len(vin_list)}] {vin}")
            retry = 0
            while retry < 2:
                browser = None
                try:
                    browser = p.chromium.launch(headless=True)
                    ctx     = browser.new_context(accept_downloads=True)
                    page    = ctx.new_page()
                    status, ket, fp = download_satu(page, vin)
                    log.append({"vin": vin, "status": status, "ket": ket, "file": fp})
                    print(f"  → {status}")
                    browser.close()
                    break
                except Exception as e:
                    print(f"  ERROR: {e}")
                    if browser:
                        browser.close()
                    retry += 1
                    if retry >= 2:
                        log.append({"vin": vin, "status": "Failed",
                                    "ket": str(e), "file": ""})

    return pd.DataFrame(log)


# ════════════════════════════════════════
# PARSE PDF
# ════════════════════════════════════════

def _safe_date(text):
    try:
        return dateparser.parse(text, dayfirst=True)
    except Exception:
        return None

def _normalize_dealer(dealer):
    if not dealer:
        return ""
    dealer = re.sub(r"\s+", " ", dealer.strip()).upper()
    if dealer == "-":
        return ""
    for k, v in {
        "NASMOCO TEGAL": "NASMOCO - TEGAL",
        "NASMOCO-TEGAL": "NASMOCO - TEGAL",
    }.items():
        dealer = dealer.replace(k, v)
    return dealer

def _extract_text(pdf_path: str) -> str:
    doc  = pymupdf.open(pdf_path)
    text = "".join(page.get_text("text") + "\n" for page in doc)
    doc.close()
    return text

def _extract_vin(text: str) -> str:
    vins = re.findall(VIN_REGEX, text)
    return vins[0] if vins else ""

def _extract_model(text: str) -> str:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    vin_idx = next(
        (i for i, l in enumerate(lines) if re.fullmatch(VIN_REGEX, l)),
        None
    )
    if vin_idx is not None:
        for j in range(vin_idx + 1, min(vin_idx + 4, len(lines))):
            c = lines[j]
            if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{4}", c): continue
            if "SAWA" in c.upper(): continue
            if re.fullmatch(VIN_REGEX, c): continue
            return c
    return ""

def _detect_validity(text: str) -> tuple:
    if "SAWA" in text.upper():
        return "VALID", "OK"
    return "INVALID", "T-Care only"

def _extract_tgl_penerimaan(text: str):
    dates = []
    for line in text.splitlines():
        try:
            dt = dateparser.parse(line.strip(), dayfirst=True, fuzzy=False)
            dates.append(dt)
        except Exception:
            pass
    return min(dates) if dates else None

def _extract_services(text: str) -> list:
    lines = [re.sub(r"\s+", " ", x.strip())
             for x in text.splitlines() if x.strip()]
    services = []
    i = 0
    while i < len(lines):
        line = lines[i]
        dm = re.match(r"^(\d{1,2}[/-]\d{1,2}[/-]\d{4})$", line)
        if dm:
            dt     = _safe_date(line)
            dealer = ""
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if not re.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{4}$", nxt):
                    dealer = nxt
            services.append((dt, _normalize_dealer(dealer)))
            i += 1
            continue
        im = re.match(r"^(\d{1,2}[/-]\d{1,2}[/-]\d{4})\s+(.+)$", line)
        if im:
            services.append((
                _safe_date(im.group(1)),
                _normalize_dealer(im.group(2))
            ))
        i += 1
    return services


def parse_folder(folder: Path) -> pd.DataFrame:
    """Parse semua PDF di folder, return DataFrame."""
    pdf_files = list(folder.glob("*.pdf"))
    print(f"\n  Parsing {len(pdf_files)} PDF...")

    vin_data = defaultdict(lambda: {
        "model": "", "tgl_penerimaan": None,
        "validity": "INVALID", "keterangan": "",
        "services": set()
    })

    for pdf_path in pdf_files:
        try:
            text    = _extract_text(str(pdf_path))
            vin     = _extract_vin(text)
            if not vin:
                continue
            model   = _extract_model(text)
            val, ket = _detect_validity(text)
            tgl     = _extract_tgl_penerimaan(text)
            svc     = _extract_services(text)
            if tgl:
                svc = [s for s in svc if s[0] != tgl]

            d = vin_data[vin]
            if model:
                d["model"] = model
            if tgl and (d["tgl_penerimaan"] is None or tgl < d["tgl_penerimaan"]):
                d["tgl_penerimaan"] = tgl
            if val == "VALID":
                d["validity"] = val
            d["keterangan"] = ket
            for s in svc:
                d["services"].add(s)

        except Exception as e:
            print(f"  ⚠ {pdf_path.name}: {e}")

    rows = []
    for vin, info in vin_data.items():
        tgl  = info["tgl_penerimaan"]
        svcs = sorted(list(info["services"]),
                      key=lambda x: x[0] or datetime.min)
        row = {
            "no_rangka":               vin,
            "model":                   info["model"],
            "tgl_penerimaan":          tgl.strftime("%Y-%m-%d") if tgl else None,
            "validity":                info["validity"],
            "keterangan":              info["keterangan"],
            "akhir_extended_warranty": (tgl + relativedelta(years=4)
                                       ).strftime("%Y-%m-%d") if tgl else None,
        }
        for i in range(7):
            if i < len(svcs):
                row[f"service_{i+1}_tgl"] = (
                    svcs[i][0].strftime("%Y-%m-%d") if svcs[i][0] else None)
                row[f"service_{i+1}_dealer"] = svcs[i][1]
            else:
                row[f"service_{i+1}_tgl"]    = None
                row[f"service_{i+1}_dealer"] = ""
        row["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        rows.append(row)

    return pd.DataFrame(rows)


# ════════════════════════════════════════
# SIMPAN KE DB + EXCEL
# ════════════════════════════════════════

def save_to_db(df: pd.DataFrame):
    conn = sqlite3.connect(DB_PATH)
    df.to_sql("extended_warranty", conn, if_exists="replace", index=False)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ew_rangka "
        "ON extended_warranty(no_rangka)"
    )
    conn.commit()
    conn.close()
    print(f"  ✅ DB updated: {len(df):,} unit")


def save_to_excel(df: pd.DataFrame):
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REKAP_FOLDER / "rekap_sawa.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="DATA", index=False)
    print(f"  ✅ Excel: {path}")
    return str(path)


# ════════════════════════════════════════
# RUN — dipanggil dari sql_agent / main
# ════════════════════════════════════════

def run(no_rangka_list: list, auto_confirm: bool = False):
    """
    Entry point dari AI agent.
    no_rangka_list : list no_rangka hasil query AI.
    auto_confirm   : True  → skip input() (dipakai oleh app.py/web)
                     False → muncul konfirmasi keyboard seperti biasa (terminal)
    """
    if not no_rangka_list:
        print("⚠ Tidak ada no_rangka untuk diproses.")
        return

    # Login Gmail sekali (optional — VIN tanpa OTP tetap diproses)
    try:
        connect_gmail()
    except Exception as e:
        print(f"  ⚠ Gmail gagal login: {e}")
        print("  VIN tanpa OTP tetap diproses")

    print(f"\n📋 {len(no_rangka_list)} unit akan dicek Extended Warranty:\n")
    for i, nr in enumerate(no_rangka_list, 1):
        print(f"  {i:3}. {nr}")

    if auto_confirm:
        konfirmasi = "ya"
    else:
        konfirmasi = input(
            f"\nDownload {len(no_rangka_list)} PDF Extended Warranty? (ya/tidak): "
        ).strip().lower()

    if konfirmasi not in ("ya", "y", "iya"):
        print("Download dibatalkan.\n")
        return

    # 1. Download PDF
    print("\n📥 Download PDF...")
    download_batch(no_rangka_list)

    # 2. Parse semua PDF di folder
    df = parse_folder(PDF_FOLDER)
    if df.empty:
        print("⚠ Tidak ada data berhasil di-parse.")
        return

    # 3. Simpan ke DB + Excel
    print("\n💾 Menyimpan data...")
    save_to_db(df)
    save_to_excel(df)

    print(f"\n✅ Selesai! {len(df):,} unit extended warranty tersimpan.\n")