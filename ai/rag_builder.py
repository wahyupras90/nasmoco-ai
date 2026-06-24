"""
ai/rag_builder.py
=================
RAG prompt builder — pilih chunk berdasarkan keyword pertanyaan.
"""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent.parent / "prompts" / "chunks"

# ── Load chunks ──
def _load(name: str) -> str:
    p = PROMPT_DIR / name
    return p.read_text(encoding='utf-8') if p.exists() else ""

CHUNK_BASE        = _load("chunk_base.txt")
CHUNK_KPI         = _load("chunk_kpi.txt")
CHUNK_TCARE       = _load("chunk_tcare.txt")
CHUNK_PARTS       = _load("chunk_parts.txt")
CHUNK_TREND       = _load("chunk_trend.txt")

# ── Keyword mapping ──
KPI_KEYWORDS = [
    'revenue','cpus','liter','ranking','pencapaian','target',
    'unit masuk','jasa','upselling','adt','tgp','sublet','invoice',
    'omset','pendapatan','kinerja','achievement','pct','persen'
]

TCARE_KEYWORDS = [
    'tcare','sbe','batas_tcare','expired','habis tcare',
    'pending sbe','sisa service','next service','aktif_kategori',
    'last_sbe','sertifikat','extended warranty','sawa'
]

PARTS_KEYWORDS = [
    'parts','oli','carbon','scc','tgp','tmo','adt','ngp',
    'nama_barang','bufferparts','sublet','ban','battery'
]

TREND_KEYWORDS = [
    'trend','yoy','ytm','ytd','per bulan','bulanan','tahunan',
    'bandingkan','compare','vs','sebelumnya','lintas bulan',
    'bulan lalu','2025','2024','dari','sampai','growth'
]


def build_prompt(pertanyaan: str) -> str:
    """
    Bangun prompt RAG dari pertanyaan.
    Selalu sertakan chunk_base, tambahkan chunk sesuai topik.
    """
    p = pertanyaan.lower()

    chunks = [CHUNK_BASE]

    if any(k in p for k in KPI_KEYWORDS):
        chunks.append(CHUNK_KPI)

    if any(k in p for k in TCARE_KEYWORDS):
        chunks.append(CHUNK_TCARE)

    if any(k in p for k in PARTS_KEYWORDS):
        chunks.append(CHUNK_PARTS)

    if any(k in p for k in TREND_KEYWORDS):
        chunks.append(CHUNK_TREND)

    return "\n\n".join(c for c in chunks if c)


def get_prompt_info(pertanyaan: str) -> dict:
    """Debug info — chunk mana yang dipilih."""
    p   = pertanyaan.lower()
    sel = {
        'kpi':   any(k in p for k in KPI_KEYWORDS),
        'tcare': any(k in p for k in TCARE_KEYWORDS),
        'parts': any(k in p for k in PARTS_KEYWORDS),
        'trend': any(k in p for k in TREND_KEYWORDS),
    }
    prompt = build_prompt(pertanyaan)
    return {
        'chunks':  [k for k, v in sel.items() if v],
        'length':  len(prompt),
        'prompt':  prompt,
    }