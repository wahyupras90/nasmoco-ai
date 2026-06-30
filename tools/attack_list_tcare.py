"""
tools/attack_list_tcare.py
===========================
Attack list TCARE — query baku, tidak bergantung LLM.
Mencegah error generate SQL untuk query yang sudah pasti formatnya.

Attack list (pending) = unit terjadwal yang belum datang
                         (bulan_realisasi IS NULL, expired=0)
Attack list expired   = unit yang batas_tcare jatuh di bulan tertentu
                         dan belum SELESAI servisnya
"""

from datetime import datetime
from db.query import run_query

VALID_SA = ['AGN', 'ARIS', 'BDR', 'IND', 'NRK', 'SAID', 'ZKY', 'KHA']
VALID_KATEGORI = ['OWN', 'BERKAH']


def _validate_bulan(bulan: str) -> str:
    """Validasi format bulan YYYY-MM, default bulan berjalan."""
    if not bulan:
        return datetime.now().strftime('%Y-%m')
    bulan = bulan.strip()
    if len(bulan) != 7 or bulan[4] != '-':
        raise ValueError(f"Format bulan tidak valid: '{bulan}'. Gunakan YYYY-MM, misal '2026-07'.")
    return bulan


def _validate_sa(sa: str) -> str:
    """Validasi kode SA."""
    sa = sa.strip().upper()
    if sa not in VALID_SA:
        raise ValueError(f"SA '{sa}' tidak dikenal. Pilihan: {', '.join(VALID_SA)}")
    return sa


def _validate_kategori(kategori: str) -> str:
    """Validasi dealer_kategori."""
    kategori = kategori.strip().upper()
    if kategori not in VALID_KATEGORI:
        raise ValueError(f"Kategori '{kategori}' tidak dikenal. Pilihan: {', '.join(VALID_KATEGORI)}")
    return kategori


def get_attack_list(bulan: str = None, sa: str = None, dealer_kategori: str = None,
                     kota: str = None, count_only: bool = False):
    """
    Ambil attack list TCARE — unit yang pending (belum datang) di bulan tertentu.

    Parameters:
        bulan           : 'YYYY-MM', default bulan berjalan
        sa              : kode SA (AGN/ARIS/BDR/IND/NRK/SAID/ZKY/KHA), default semua
        dealer_kategori : 'OWN' atau 'BERKAH', default semua
        kota            : nama kota (untuk filter BERKAH), default semua
        count_only      : True → return jumlah saja (int), False → return DataFrame

    Returns:
        DataFrame (kolom: no_rangka, dealer_kategori, pekerjaan, bulan_jadwal,
                   customer, sa_terakhir, batas_tcare, kunjungan)
        atau int (jika count_only=True)

    Semua input divalidasi via whitelist (_validate_*) sebelum masuk SQL —
    aman dari SQL injection meski run_query() tidak support parameter binding.
    """
    bulan = _validate_bulan(bulan)

    where = [
        f"ts.bulan_jadwal = '{bulan}'",
        "ts.bulan_realisasi IS NULL",
        "ts.expired = 0"
    ]

    if sa:
        sa = _validate_sa(sa)
        where.append(f"ut.sa_terakhir = '{sa}'")

    if dealer_kategori:
        dealer_kategori = _validate_kategori(dealer_kategori)
        where.append(f"ts.dealer_kategori = '{dealer_kategori}'")

    if kota:
        kota_safe = kota.strip().upper().replace("'", "")
        where.append(f"UPPER(ut.kota) LIKE '%{kota_safe}%'")

    where_clause = " AND ".join(where)

    if count_only:
        sql = f"""
            SELECT COUNT(*) AS total
            FROM tcare_schedule ts
            JOIN unit_tcare ut ON ts.no_rangka = ut.no_rangka
            WHERE {where_clause}
        """
        result = run_query(sql)
        return int(result.iloc[0]['total'])

    sql = f"""
        SELECT ts.no_rangka, ut.tcare_type, ts.dealer_kategori, ts.pekerjaan, ts.bulan_jadwal, ts.kunjungan,
               ut.customer, ut.sa_terakhir, ut.batas_tcare, ut.sisa_detail
        FROM tcare_schedule ts
        JOIN unit_tcare ut ON ts.no_rangka = ut.no_rangka
        WHERE {where_clause}
        ORDER BY ts.dealer_kategori, ts.pekerjaan
    """
    return run_query(sql)


def get_attack_list_expired(bulan: str = None, sa: str = None, dealer_kategori: str = None,
                             kota: str = None, count_only: bool = False):
    """
    Ambil attack list TCARE EXPIRED — unit yang batas_tcare jatuh di bulan tertentu
    dan belum menyelesaikan servisnya (sisa_detail != 'SELESAI').

    Tidak terikat ke tcare_schedule.bulan_jadwal sama sekali — murni filter
    berdasarkan unit_tcare.batas_tcare. Tidak pakai kolom 'expired' sama sekali
    (kolom itu tidak ada di unit_tcare).

    Parameters sama seperti get_attack_list().

    Returns:
        DataFrame (kolom: no_rangka, dealer_kategori, customer, sa_terakhir,
                   batas_tcare, sisa_detail)
        atau int (jika count_only=True)
    """
    bulan = _validate_bulan(bulan)

    where = [
        f"strftime('%Y-%m', ut.batas_tcare) = '{bulan}'",
        "(ut.sisa_detail IS NULL OR ut.sisa_detail != 'SELESAI')"
    ]

    if sa:
        sa = _validate_sa(sa)
        where.append(f"ut.sa_terakhir = '{sa}'")

    if dealer_kategori:
        dealer_kategori = _validate_kategori(dealer_kategori)
        where.append(f"ut.dealer_kategori = '{dealer_kategori}'")

    if kota:
        kota_safe = kota.strip().upper().replace("'", "")
        where.append(f"UPPER(ut.kota) LIKE '%{kota_safe}%'")

    where_clause = " AND ".join(where)

    if count_only:
        sql = f"""
            SELECT COUNT(*) AS total
            FROM unit_tcare ut
            WHERE {where_clause}
        """
        result = run_query(sql)
        return int(result.iloc[0]['total'])

    sql = f"""
        SELECT ut.no_rangka, ut.dealer_kategori, ut.customer, ut.sa_terakhir,
               ut.batas_tcare, ut.sisa_detail
        FROM unit_tcare ut
        WHERE {where_clause}
        ORDER BY ut.dealer_kategori, ut.batas_tcare
    """
    return run_query(sql)


# ════════════════════════════════════════
# PARSER — deteksi dari teks bebas
# ════════════════════════════════════════

BULAN_MAP = {
    'januari': '01', 'jan': '01',
    'februari': '02', 'feb': '02',
    'maret': '03', 'mar': '03',
    'april': '04', 'apr': '04',
    'mei': '05',
    'juni': '06', 'jun': '06',
    'juli': '07', 'jul': '07',
    'agustus': '08', 'agt': '08', 'aug': '08',
    'september': '09', 'sep': '09',
    'oktober': '10', 'okt': '10', 'oct': '10',
    'november': '11', 'nov': '11',
    'desember': '12', 'des': '12', 'dec': '12',
}


def parse_bulan_dari_teks(text: str) -> str:
    """
    Parse nama bulan + tahun dari teks bebas → 'YYYY-MM'.
    Contoh: 'juli 26' -> '2026-07', 'juli 2026' -> '2026-07'
    Return None kalau tidak ditemukan (pakai bulan berjalan).
    """
    import re
    t = text.lower()

    nama_bulan = None
    for nama, num in BULAN_MAP.items():
        if nama in t:
            nama_bulan = num
            break

    if not nama_bulan:
        return None

    # Cari tahun: 4 digit (2026) atau 2 digit (26)
    yr_match = re.search(r'\b(20\d{2})\b', t)
    if yr_match:
        tahun = yr_match.group(1)
    else:
        yr_match2 = re.search(r'\b(\d{2})\b', t)
        tahun = f"20{yr_match2.group(1)}" if yr_match2 else str(datetime.now().year)

    return f"{tahun}-{nama_bulan}"


def parse_sa_dari_teks(text: str) -> str:
    """Deteksi kode SA dari teks bebas. Return None kalau tidak ada."""
    t = text.upper()
    for sa in VALID_SA:
        if sa in t.split():
            return sa
    # Cek tanpa split (untuk kasus "sa bdr" nempel)
    for sa in VALID_SA:
        if f" {sa} " in f" {t} " or t.endswith(f" {sa}"):
            return sa
    return None


def is_attack_list_query(text: str) -> bool:
    """Deteksi apakah pertanyaan adalah attack list TCARE."""
    t = text.lower()
    return 'attack list' in t and 'tcare' in t


def is_expired_query(text: str) -> bool:
    """Deteksi apakah attack list yang diminta mode expired (berdasarkan batas_tcare)."""
    return 'expired' in text.lower()


def handle_attack_list(pertanyaan: str):
    """
    Entry point dari main.py — parse teks bebas dan jalankan get_attack_list()
    atau get_attack_list_expired() tergantung kata 'expired' di teks.
    """
    bulan = parse_bulan_dari_teks(pertanyaan)
    sa    = parse_sa_dari_teks(pertanyaan)
    expired_mode = is_expired_query(pertanyaan)

    count_only = any(k in pertanyaan.lower() for k in ['berapa', 'jumlah', 'total', 'ada berapa'])

    bulan_label = bulan or datetime.now().strftime('%Y-%m')
    label_mode  = "EXPIRED" if expired_mode else ""
    fn          = get_attack_list_expired if expired_mode else get_attack_list

    if count_only:
        total = fn(bulan=bulan, sa=sa, count_only=True)
        label = f" SA {sa}" if sa else ""
        print(f"\nAttack list TCARE {label_mode} {bulan_label}{label}: {total} unit\n")
        return

    df = fn(bulan=bulan, sa=sa)
    if df.empty:
        print(f"\nTidak ada attack list TCARE {label_mode} untuk {bulan_label}" +
              (f" SA {sa}" if sa else "") + ".\n")
        return

    print(f"\nAttack list TCARE {label_mode} {bulan_label}" + (f" SA {sa}" if sa else "") +
          f" — {len(df)} unit:\n")
    print(df.to_string(index=False))
    print()