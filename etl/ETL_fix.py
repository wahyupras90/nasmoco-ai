"""
ETL Nasmoco Tegal
=================
Input  : File mentah DMS (unit masuk, invoice, parts SCC, RS) + master files
Output : Output\nasmoco.db  (SQLite — untuk AI Agent)

Cara pakai:
  python ETL_Nasmoco_Tegal.py      → jalankan ETL
  atau klik run_etl.bat
"""

import pandas as pd
import sqlite3
import re
import time
import warnings
from pathlib import Path
from glob import glob

warnings.filterwarnings('ignore')


# ════════════════════════════════════════════════════════════
# CONFIG FOLDER
# ════════════════════════════════════════════════════════════

BASE = r"D:\Database Unit Masuk"
AI_BASE = r"D:\AI_nasmoco"
PATHS = {
    "unit_masuk":  BASE + r"\Unit Masuk Harian",
    "invoice":     BASE + r"\Invoice",
    "parts_baru":  BASE + r"\Parts\baru",
    "parts_cache": BASE + r"\Parts\cache",
    "master":      BASE + r"\Master",
    "rs":          BASE + r"\RS",
    "output":      AI_BASE + r"\db",
}


# ════════════════════════════════════════════════════════════
# HELPER
# ════════════════════════════════════════════════════════════

def get_excel_files(folder: str) -> list:
    files = []
    for ext in ('*.xls', '*.xlsx', '*.xlsm'):
        files += glob(f"{folder}/**/{ext}", recursive=True)
    # Skip temp files (~$...)
    return sorted(f for f in set(files)
                  if not Path(f).name.startswith('~$'))

def s(v):
    return str(v).strip() if not pd.isna(v) else ''

def n(v):
    return v if not pd.isna(v) else 0


# ════════════════════════════════════════════════════════════
# 1. MASTER FILES
# ════════════════════════════════════════════════════════════

def load_masters(folder: str) -> tuple:
    path = Path(folder)
    df_oli = pd.read_excel(path / 'master oli.xlsx',
                           sheet_name='Sheet1', engine='openpyxl')
    oli_map = {
        str(r['Pno']).strip(): {
            'Tipe': r['Tipe'], 'kemasan': r['kemasan'], 'jenis': r['jenis']
        }
        for _, r in df_oli.iterrows()
    }
    df_sub = pd.read_excel(path / 'master sublet.xlsx',
                           sheet_name='master sublet', engine='openpyxl')
    sublet_map = {
        str(r['Pekerjaan']).upper().strip(): r['Sublet']
        for _, r in df_sub.iterrows()
    }
    return oli_map, sublet_map


# ════════════════════════════════════════════════════════════
# 2. LOAD RS (untuk enrich unitmasuk + simpan ke DB)
# ════════════════════════════════════════════════════════════

def _parse_rs_file(filepath: str) -> pd.DataFrame:
    """
    Parse satu file RS. Handle 2 format:
    - Format lama : kolom No_Rangka, Tgl_DO, Nama_Sales, No_Polisi, Telp_GSM, Nama_Customer
    - Format baru : kolom No. Rangka, Tgl BPK, Nama Sales, dst
    Return DataFrame dengan kolom standar.
    """
    df = pd.read_excel(filepath, header=1, engine='openpyxl')
    cols = set(df.columns.astype(str).str.strip())

    # FORMAT LAMA
    if {'No_Rangka', 'Tgl_DO', 'Nama_Sales'}.issubset(cols):
        rename = {
            'No_Rangka':     'no_rangka',
            'Tgl_DO':        'tgl_do',
            'Nama_Sales':    'nama_sales',
            'No_Polisi':     'no_polisi',
            'Telp_GSM':      'telp_gsm',
            'Nama_Customer': 'customer',
        }

    # FORMAT BARU (2026)
    elif {'No. Rangka', 'Tgl BPK', 'Nama Sales'}.issubset(cols):
        rename = {
            'No. Rangka':  'no_rangka',
            'Tgl BPK':     'tgl_do',
            'Nama Sales':  'nama_sales',
            'No. Polisi':  'no_polisi',
            'Telp GSM':    'telp_gsm',
            'Nama Customer': 'customer',
        }
    else:
        raise ValueError(f"Format tidak dikenali. Kolom: {cols}")

    # Ambil kolom yang ada saja (graceful)
    keep = {k: v for k, v in rename.items() if k in cols}
    df = df[list(keep.keys())].copy()
    df = df.rename(columns=keep)
    return df


def load_rs(folder: str) -> pd.DataFrame:
    """Untuk enrich unitmasuk — hanya butuh no_rangka, tgl_do, batas_tcare, nama_sales."""
    files = get_excel_files(folder)
    empty = pd.DataFrame(columns=['no_rangka', 'tgl_do', 'batas_tcare', 'nama_sales'])
    if not files:
        return empty

    dfs = []
    for f in files:
        try:
            dfs.append(_parse_rs_file(f))
        except Exception as e:
            print(f"  ⚠ RS skip {Path(f).name}: {e}")

    if not dfs:
        return empty

    df = pd.concat(dfs, ignore_index=True)
    df['tgl_do']    = pd.to_datetime(df['tgl_do'], errors='coerce')
    df['no_rangka'] = df['no_rangka'].astype(str).str.strip()
    df = df.dropna(subset=['no_rangka', 'tgl_do'])
    df = (df.sort_values('tgl_do', ascending=False)
            .drop_duplicates(subset='no_rangka', keep='first')
            .reset_index(drop=True))
    df['batas_tcare'] = (
        (df['tgl_do'] + pd.DateOffset(months=36))
        .dt.to_period('M')
        .dt.to_timestamp('M')  # ambil hari terakhir bulan
        )
    return df[['no_rangka', 'tgl_do', 'batas_tcare', 'nama_sales']]


def load_rs_full(folder: str) -> pd.DataFrame:
    """Untuk tabel rs di DB — kolom lengkap termasuk no_polisi, telp_gsm, customer."""
    files = get_excel_files(folder)
    if not files:
        return pd.DataFrame()

    dfs = []
    for f in files:
        try:
            dfs.append(_parse_rs_file(f))
        except Exception as e:
            print(f"  ⚠ RS skip {Path(f).name}: {e}")

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df['tgl_do']    = pd.to_datetime(df['tgl_do'], errors='coerce')
    df['no_rangka'] = df['no_rangka'].astype(str).str.strip()
    df = df.dropna(subset=['no_rangka', 'tgl_do'])
    df = (df.sort_values('tgl_do', ascending=False)
            .drop_duplicates(subset='no_rangka', keep='first')
            .reset_index(drop=True))

    # Kolom turunan
    df['batas_tcare'] = (
    (df['tgl_do'] + pd.DateOffset(months=36))
    .dt.to_period('M')
    .dt.to_timestamp('M')  # ambil hari terakhir bulan
    )
    df['in_tcare']         = (df['batas_tcare'] >= pd.Timestamp.today()).astype(int)
    df['sisa_bulan_tcare'] = (
        (df['batas_tcare'] - pd.Timestamp.today()) / pd.Timedelta(days=30)
    ).round(1).clip(lower=0)

    # Tanggal ke string
    df['tgl_do']      = df['tgl_do'].dt.strftime('%Y-%m-%d')
    df['batas_tcare'] = df['batas_tcare'].dt.strftime('%Y-%m-%d')

    # Kolom final — isi NaN untuk kolom yang tidak ada di format tertentu
    for col in ['no_polisi', 'telp_gsm', 'customer', 'nama_sales']:
        if col not in df.columns:
            df[col] = None

    return df[['no_rangka', 'customer', 'tgl_do', 'no_polisi',
               'telp_gsm', 'nama_sales', 'batas_tcare',
               'in_tcare', 'sisa_bulan_tcare']]


# ════════════════════════════════════════════════════════════
# 3. PARSE INVOICE
# ════════════════════════════════════════════════════════════

BULAN_MAP = {
    'Januari':'January','Februari':'February','Maret':'March',
    'April':'April','Mei':'May','Juni':'June','Juli':'July',
    'Agustus':'August','September':'September','Oktober':'October',
    'November':'November','Desember':'December'
}

def parse_tanggal_invoice(teks: str):
    raw = teks.replace('Sub Total tanggal ','').strip()
    for id_b, en_b in BULAN_MAP.items():
        raw = raw.replace(id_b, en_b)
    try:
        return pd.to_datetime(raw, format='%d-%B-%Y').date()
    except:
        return None

def parse_satu_invoice(filepath: str) -> pd.DataFrame:
    engine = 'xlrd' if filepath.endswith('.xls') else 'openpyxl'
    df_raw = pd.read_excel(filepath, sheet_name=0, engine=engine, header=None)
    rows = []
    for _, row in df_raw.iterrows():
        v0 = row.iloc[0]
        if isinstance(v0, (int, float)) and not pd.isna(v0):
            try:
                if int(v0) > 0:
                    rows.append({
                        'no_invoice':  str(row.iloc[1]).strip(),
                        'no_wo':       str(row.iloc[5]).strip(),
                        'jasa':        row.iloc[14] or 0,
                        'sublet':      row.iloc[18] or 0,
                        'materai':     row.iloc[19] or 0,
                        'invoice':     row.iloc[20] or 0,
                        'tgl_invoice': None,
                    })
            except: pass
        else:
            for v in row.tolist():
                if 'Sub Total tanggal' in str(v):
                    tgl = parse_tanggal_invoice(str(v))
                    for r in reversed(rows):
                        if r['tgl_invoice'] is None:
                            r['tgl_invoice'] = tgl
                        else:
                            break
                    break
    return pd.DataFrame(rows)

def load_invoice(folder: str) -> pd.DataFrame:
    files = get_excel_files(folder)
    dfs = []
    for f in files:
        try:
            dfs.append(parse_satu_invoice(f))
        except Exception as e:
            print(f"  ⚠ Invoice skip {Path(f).name}: {e}")
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    df['no_wo']       = pd.to_numeric(df['no_wo'], errors='coerce')
    df['tgl_invoice'] = pd.to_datetime(df['tgl_invoice'])
    grp = df.groupby('no_wo').agg(
        invoice     =('invoice',   'sum'),
        jasa        =('jasa',      'sum'),
        sublet      =('sublet',    'sum'),
        materai     =('materai',   'sum'),
        tgl_invoice =('tgl_invoice','first'),
        no_invoice  =('no_invoice', 'first'),
    ).reset_index()
    return grp


# ════════════════════════════════════════════════════════════
# 4. PARSE UNIT MASUK
# ════════════════════════════════════════════════════════════

def parse_satu_unitmasuk(filepath: str) -> pd.DataFrame:
    engine = 'xlrd' if filepath.endswith('.xls') else 'openpyxl'
    df_raw = pd.read_excel(filepath, sheet_name=0, engine=engine, header=None)
    rows = []
    current_date = None
    current_wo   = {}
    current_mech = ''

    for _, row in df_raw.iterrows():
        vals = row.tolist()
        v0   = str(vals[0]).strip() if vals[0] is not None else ''

        if 'Tanggal Service' in v0:
            current_date = vals[2]
            continue

        try:
            seq = int(float(v0))
            if seq > 0 and not pd.isna(vals[1]):
                current_mech = s(vals[13])
                current_wo = {
                    'no_polisi': s(vals[1]),  'no_wo':    s(vals[2]),
                    'customer':  s(vals[4]),
                    'jam':       s(vals[5]),  'serah':    s(vals[6]),
                    'no_rangka': s(vals[7]),  'model':    s(vals[8]),
                    'tunggu':    s(vals[11]), 'sa':       s(vals[12]),
                    'tanggal':   current_date,
                }
                rows.append({**current_wo,
                    'mech':      current_mech,
                    'klp':       s(vals[14]),
                    'pekerjaan': s(vals[15]),
                    'rate':      n(vals[16]), 'arate': n(vals[17]),
                    'stat':      s(vals[18]), 'ket':   s(vals[19]),
                })
                continue
        except (ValueError, TypeError):
            pass

        if s(vals[13]) and s(vals[13]) != 'nan' and current_wo:
            current_mech = s(vals[13])
            rows.append({**current_wo,
                'mech':      current_mech,
                'klp':       s(vals[14]),
                'pekerjaan': s(vals[15]),
                'rate':      n(vals[16]), 'arate': n(vals[17]),
                'stat':      s(vals[18]), 'ket':   '',
            })
            continue

        if s(vals[14]) == 'SUB' and current_wo:
            rows.append({**current_wo,
                'mech':      current_mech,
                'klp':       'SUB',
                'pekerjaan': s(vals[15]),
                'rate':      0, 'arate': 0,
                'stat':      s(vals[18]), 'ket': '',
            })

    return pd.DataFrame(rows)

def load_unitmasuk(folder: str) -> pd.DataFrame:
    files = get_excel_files(folder)
    dfs = []
    for f in files:
        try:
            dfs.append(parse_satu_unitmasuk(f))
        except Exception as e:
            print(f"  ⚠ Unit Masuk skip {Path(f).name}: {e}")
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=['no_wo', 'pekerjaan'])

    # Fallback KLP untuk pekerjaan yang tidak diisi di DMS
    PEKERJAAN_KLP_MAP = {
        'HYBRID HEALTH CHECK':      'GRP',
        'ROOF BOX':                 'GRP',
        'BRAKE ALL ADJUSMENT':      'GRP',
        'FUEL FILTER  REPAIR':      'GRP',
        'UNDERSTEEL & ENGINE CHECK':'GRP',
    }
    mask_kosong = df['klp'].isna() | (df['klp'].str.strip() == '')
    df.loc[mask_kosong, 'klp'] = df.loc[mask_kosong, 'pekerjaan'].apply(
        lambda p: PEKERJAAN_KLP_MAP.get(str(p).strip().upper(), '')
    )

    return df.reset_index(drop=True)


# ════════════════════════════════════════════════════════════
# 5. ENRICH UNITMASUK
# ════════════════════════════════════════════════════════════

KLP_POINT = {'SBI':5,'SBE':4,'LUB':3,'GRP':2,'SUB':1}

def enrich_unitmasuk(df: pd.DataFrame, sublet_map: dict,
                     df_invoice: pd.DataFrame,
                     df_rs: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Sublet type dari master
    df['sublet_type'] = df['pekerjaan'].str.upper().str.strip().map(sublet_map)

    # Kelompok per WO
    df['_pt']  = df['klp'].str.upper().str.strip().map(KLP_POINT).fillna(0).astype(int)
    max_pt     = df.groupby('no_wo')['_pt'].max().rename('_max_pt')
    df         = df.merge(max_pt, on='no_wo', how='left')

    def get_kelompok(row):
        if row['stat'] == 'W': return 'WRT'
        p = row['_max_pt']
        if p == 5: return 'SBI'
        if p == 4: return 'SBE'
        if p == 3: return 'LUB'
        if p in (2,1): return 'GRP'
        return row['klp']

    df['kelompok'] = df.apply(get_kelompok, axis=1)
    df['kategori'] = df.apply(
        lambda r: 'non CPUS'
        if r['kelompok'] in ('SBI','PDS') or r['stat'] == 'W'
        else 'CPUS', axis=1)

    # TCARE
    df['_is_tcare'] = (
        df['customer'].str.lower().str.contains('new ratna', na=False) &
        (df['klp'].str.lower().str.strip() == 'sbe')
    )
    has_tcare  = df.groupby('no_wo')['_is_tcare'].any().rename('_has_tcare')
    df         = df.merge(has_tcare, on='no_wo', how='left')
    df['tcare'] = df['_has_tcare'].map({True:'TCARE', False:'REGULER'})

    # Join invoice
    df['_wo_num'] = pd.to_numeric(df['no_wo'], errors='coerce')
    inv_slim = df_invoice[['no_wo','no_invoice','tgl_invoice']].rename(
        columns={'no_wo':'_wo_num'})
    df = df.merge(inv_slim, on='_wo_num', how='left')

    # Join RS
    if len(df_rs) > 0:
        rs_slim = df_rs.rename(columns={
            'no_rangka':   'no_rangka',
            'tgl_do':      'tgl_bpk',
            'batas_tcare': 'batas_tcare',
            'nama_sales':  'nama_sales',
        })
        df = df.merge(rs_slim, on='no_rangka', how='left')
        df['is_own']   = df['tgl_bpk'].notna().astype(int)
        df['in_tcare'] = (
            df['batas_tcare'].notna() &
            (df['batas_tcare'] >= pd.Timestamp.today())
        ).astype(int)
    else:
        df['is_own']      = 0
        df['tgl_bpk']     = pd.NaT
        df['batas_tcare'] = pd.NaT
        df['nama_sales']  = None
        df['in_tcare']    = 0

    # Cleanup temp cols
    df = df.drop(columns=[c for c in
        ['_pt','_max_pt','_is_tcare','_has_tcare','_wo_num']
        if c in df.columns])

    # Tipe data
    df['no_wo']    = pd.to_numeric(df['no_wo'], errors='coerce')
    df['rate']     = pd.to_numeric(df['rate'],  errors='coerce')
    df['arate']    = pd.to_numeric(df['arate'], errors='coerce')
    df['tanggal']  = pd.to_datetime(df['tanggal'], errors='coerce').dt.strftime('%Y-%m-%d')
    df['tgl_invoice']  = pd.to_datetime(df['tgl_invoice'],  errors='coerce').dt.strftime('%Y-%m-%d')
    df['tgl_bpk']      = pd.to_datetime(df['tgl_bpk'],      errors='coerce').dt.strftime('%Y-%m-%d')
    df['batas_tcare']  = pd.to_datetime(df['batas_tcare'],  errors='coerce').dt.strftime('%Y-%m-%d')

    # Reorder kolom
    cols = ['no_polisi','no_wo','no_invoice','customer',
            'jam','serah','no_rangka','model','tunggu','sa','mech',
            'klp','pekerjaan','rate','arate','stat','ket','tanggal',
            'tgl_invoice','sublet_type','kategori','kelompok','tcare',
            'is_own','tgl_bpk','batas_tcare','in_tcare','nama_sales']
    return df[[c for c in cols if c in df.columns]]


# ════════════════════════════════════════════════════════════
# 6. PARSE PARTS → bufferparts
# ════════════════════════════════════════════════════════════

def normalize_pno(kode: str) -> str:
    """
    Strip prefix 1 karakter sebelum dash pertama.
    A-08880-85476  → 08880-85476  ✓
    1-08880-85476  → 08880-85476  ✓
    08880-85476    → 08880-85476  ✓ (tanpa prefix, tidak diubah)
    08880-85367-TG → 08880-85367-TG ✓ (suffix tetap)
    """
    k = str(kode).strip()
    m = re.match(r'^[A-Za-z0-9]-(.+)', k)
    return m.group(1) if m else k

def parse_satu_parts(filepath: str) -> pd.DataFrame:
    engine = 'xlrd' if filepath.endswith('.xls') else 'openpyxl'
    df_raw = pd.read_excel(filepath, sheet_name='G', engine=engine, header=None)
    rows = []
    for _, row in df_raw.iterrows():
        vals = row.tolist()
        v0   = vals[0]
        if not isinstance(v0, (int,float)) or pd.isna(v0):
            continue
        try:
            if int(v0) < 1: continue
        except: continue

        no_faktur = s(vals[2])
        kode_cust = s(vals[3])
        dpp       = vals[15] if not pd.isna(vals[15]) else 0

        if kode_cust.upper().startswith('AFLNSC'):
            invoice_val = dpp           # AFLNSC: tidak kena PPN
        else:
            invoice_val = dpp * 1.11    # semua lainnya: + PPN 11%


        rows.append({
            'tgl_faktur':  vals[1],
            'no_faktur':   no_faktur,
            'kode_cust':   kode_cust,
            'no_wo':       s(vals[4]),
            'kode_item':   s(vals[5]),
            'nama_barang': s(vals[6]),
            'scc':         s(vals[9]),
            'satuan':      s(vals[10]),
            'qty':         vals[12] if not pd.isna(vals[12]) else 0,
            'disc':        vals[13] if not pd.isna(vals[13]) else 0,
            'invoice_dpp': dpp,
            'invoice':     invoice_val,
            'harga_pokok': vals[18] if not pd.isna(vals[18]) else 0,
            'laba':        vals[19] if not pd.isna(vals[19]) else 0,
        })
    return pd.DataFrame(rows)

def load_parts(folder_baru: str, folder_cache: str,
               oli_map: dict, df_unitmasuk: pd.DataFrame) -> pd.DataFrame:
    files = get_excel_files(folder_baru) + get_excel_files(folder_cache)
    dfs = []
    for f in files:
        try:
            dfs.append(parse_satu_parts(f))
        except Exception as e:
            print(f"  ⚠ Parts skip {Path(f).name}: {e}")
    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=['no_faktur','kode_item'])

    # Filter counter SCC dan faktur non-D
    df['_s'] = df['scc'].str.upper().fillna('')
    df['_f'] = df['no_faktur'].str.upper().fillna('')
    df = df[~(df['_s'].str.contains('2') & ~df['_f'].str.startswith('D'))]
    df = df[df['no_wo'].apply(
        lambda x: not bool(re.search(r'W', str(x))) if x else True)]
    df = df.drop(columns=['_s','_f'])

    # Tipe data
    df['tgl_faktur'] = df['tgl_faktur'].apply(
        lambda x: pd.to_datetime(str(x).replace(' 00:00:00',''), errors='coerce'))
    df['qty']     = pd.to_numeric(df['qty'],     errors='coerce')
    df['invoice'] = pd.to_numeric(df['invoice'], errors='coerce')

    # Join master oli
    df['_pno'] = df['kode_item'].apply(normalize_pno)
    df['oli_tipe']    = df['_pno'].map(lambda x: oli_map.get(x,{}).get('Tipe'))
    df['oli_kemasan'] = df['_pno'].map(lambda x: oli_map.get(x,{}).get('kemasan'))
    df['oli_jenis']   = df['_pno'].map(lambda x: oli_map.get(x,{}).get('jenis'))
    df['total_liter'] = df.apply(
        lambda r: r['qty'] * r['oli_kemasan']
        if pd.notna(r['oli_kemasan']) else None, axis=1)
    df = df.drop(columns=['_pno'])

    # Join SA & pekerjaan dari unitmasuk
    wo_info = (df_unitmasuk[df_unitmasuk['klp'] != 'SUB']
               .groupby('no_wo')[['sa','pekerjaan']].first()
               .reset_index()
               .rename(columns={'no_wo':'_wo_num'}))
    df['_wo_num'] = pd.to_numeric(df['no_wo'], errors='coerce')
    df = df.merge(wo_info, on='_wo_num', how='left')
    df = df.drop(columns=['_wo_num'])

    # String tanggal untuk SQLite
    df['tgl_faktur'] = df['tgl_faktur'].dt.strftime('%Y-%m-%d')
    return df


# ════════════════════════════════════════════════════════════
# 7. BUILD REKAPBULANAN
# ════════════════════════════════════════════════════════════

SCC_TGP = {'TGP','TLS','SST','TMO','TMS','TMF','TGG'}

def scc_kat(scc: str, kode: str) -> str:
    p = str(scc).strip()[:3].upper()
    if p == 'ADT': return 'ADT'
    if p in SCC_TGP: return 'TGP'
    if p == 'BAN' and len(str(kode)) >= 13: return 'TGP'
    return 'OTH'

def build_rekapbulanan(df_invoice, df_parts, df_unitmasuk) -> pd.DataFrame:
    wo_info = df_unitmasuk.groupby('no_wo').agg(
        sa      =('sa',       'first'),
        kelompok=('kelompok', 'first'),
        tcare   =('tcare',    'first'),
        model   =('model',    'first'),
    ).reset_index()

    base = df_invoice.merge(wo_info, on='no_wo', how='left')

    # Parts kategori
    parts = df_parts.copy()
    parts['_kat'] = parts.apply(lambda r: scc_kat(r['scc'], r['kode_item']), axis=1)
    parts['_tgp'] = parts['invoice'].where(parts['_kat']=='TGP', 0)
    parts['_adt'] = parts['invoice'].where(parts['_kat']=='ADT', 0)
    parts['_oth'] = parts['invoice'].where(parts['_kat']=='OTH', 0)
    parts['_ctr'] = parts['no_wo'].isna() | (parts['no_wo'].str.strip()=='')

    non_ctr = parts[~parts['_ctr']].copy()
    non_ctr['_wo'] = pd.to_numeric(non_ctr['no_wo'], errors='coerce')
    grp = non_ctr.groupby('_wo').agg(
        tgp        =('_tgp','sum'),
        adt        =('_adt','sum'),
        oth        =('_oth','sum'),
        total_liter=('total_liter','sum'),
        tgl_faktur =('tgl_faktur','max'),
    ).reset_index().rename(columns={'_wo':'no_wo'})

    merged = base.merge(grp, on='no_wo', how='left')
    merged['total_revenue'] = (
        merged['jasa'].fillna(0)   + merged['sublet'].fillna(0) +
        merged['tgp'].fillna(0)    + merged['adt'].fillna(0)    +
        merged['oth'].fillna(0))
    merged['tanggal'] = pd.to_datetime(
        merged['tgl_invoice'], errors='coerce').combine_first(
        pd.to_datetime(merged['tgl_faktur'], errors='coerce'))

    def kat_rekap(r):
        if pd.isna(r['no_wo']): return 'Counter'
        if str(r.get('tcare','')).upper() == 'TCARE': return 'TCARE'
        return r.get('kelompok','')

    merged['kategori']   = merged.apply(kat_rekap, axis=1)
    merged['is_counter'] = (merged['sa'] == 'Counter').astype(int)
    merged['status_hari'] = pd.to_datetime(merged['tanggal'], errors='coerce').apply(
        lambda d: 'Hari Libur' if pd.notna(d) and d.weekday()==6 else 'Hari Kerja')

    # Counter parts
    ctr = parts[parts['_ctr']].copy()
    result_list = [merged]
    if len(ctr) > 0:
        ctr_grp = ctr.groupby('no_faktur').agg(
            total_revenue=('invoice','sum'),
            total_liter  =('total_liter','sum'),
            tgp=('_tgp','sum'), adt=('_adt','sum'), oth=('_oth','sum'),
            tanggal      =('tgl_faktur','max'),
        ).reset_index()
        ctr_grp['sa']          = 'Counter'
        ctr_grp['no_wo']       = None
        ctr_grp['jasa']        = None
        ctr_grp['sublet']      = None
        ctr_grp['kategori']    = 'Counter'
        ctr_grp['is_counter']  = 1
        ctr_grp['status_hari'] = 'Hari Kerja'
        ctr_grp['invoice']     = ctr_grp['total_revenue']
        ctr_grp = ctr_grp.rename(columns={'no_faktur':'no_invoice'})
        result_list.append(ctr_grp)

    final_cols = ['tanggal','no_wo','sa','model','no_invoice',
                  'invoice','jasa','sublet','tgp','adt','oth',
                  'total_liter','total_revenue','kategori',
                  'is_counter','status_hari']
    result = pd.concat(result_list, ignore_index=True)
    result = result[[c for c in final_cols if c in result.columns]]

    # Tipe data final
    result['no_wo']        = pd.to_numeric(result['no_wo'], errors='coerce')
    result['tanggal']      = pd.to_datetime(result['tanggal'], errors='coerce').dt.strftime('%Y-%m-%d')
    result['invoice']      = pd.to_numeric(result['invoice'],       errors='coerce')
    result['jasa']         = pd.to_numeric(result['jasa'],          errors='coerce')
    result['sublet']       = pd.to_numeric(result['sublet'],        errors='coerce')
    result['tgp']          = pd.to_numeric(result['tgp'],           errors='coerce')
    result['adt']          = pd.to_numeric(result['adt'],           errors='coerce')
    result['oth']          = pd.to_numeric(result['oth'],           errors='coerce')
    result['total_liter']  = pd.to_numeric(result['total_liter'],   errors='coerce')
    result['total_revenue']= pd.to_numeric(result['total_revenue'], errors='coerce')

    return result.sort_values(['no_wo','sa','tanggal'], na_position='last')


# ════════════════════════════════════════════════════════════
# 8. LOAD TARGET
# ════════════════════════════════════════════════════════════

def load_target_manual(master_folder: str,
                       df_unitmasuk: pd.DataFrame = None) -> pd.DataFrame:
    """
    Baca target dari target_manual.xlsx (total outlet + KHA).
    ETL hitung per SA = (total - KHA) / jumlah SA aktif.
    SA aktif = SA yang muncul di unitmasuk 14 hari terakhir (kecuali KHA).
    Fleksibel: kalau ZKY pindah ke Brebes, otomatis n_SA berkurang.
    """
    target_file = Path(master_folder) / 'target_manual.xlsx'
    if not target_file.exists():
        print(f'  ⚠ target_manual.xlsx tidak ditemukan')
        return pd.DataFrame()

    df_tgt = pd.read_excel(target_file, sheet_name='target',
                           engine='openpyxl', header=4)

    # Normalize kolom → snake_case
    df_tgt.columns = (df_tgt.columns.str.strip()
                                    .str.lower()
                                    .str.replace(r'[^a-z0-9]+', '_', regex=True)
                                    .str.strip('_'))
    col_map = {
        'tahun':            'tahun',
        'bulan':            'bulan',
        'nama_bulan':       'bulan_nama',
        'cpus_total':       'cpus_total',
        'revenue_total_rp': 'revenue_total',
        'liter_total':      'liter_total',
        'cpus_kha':         'cpus_kha',
        'revenue_kha_rp':   'revenue_kha',
        'liter_kha':        'liter_kha',
    }
    df_tgt = df_tgt.rename(columns=col_map)
    df_tgt = df_tgt.dropna(subset=['cpus_total'])

    # Deteksi SA aktif dari unitmasuk 14 hari terakhir
    SA_EXCLUDE = {'KHA', 'TOTAL', 'Counter', ''}
    if df_unitmasuk is not None and len(df_unitmasuk) > 0:
        cutoff = pd.Timestamp.today() - pd.DateOffset(days=14)
        df_recent = df_unitmasuk[
            pd.to_datetime(df_unitmasuk['tanggal'], errors='coerce') >= cutoff
        ]
        sa_aktif = [sa for sa in df_recent['sa'].str.strip().unique()
                    if sa not in SA_EXCLUDE and sa]
    else:
        sa_aktif = ['AGN','ARIS','BDR','IND','NRK','SAID','ZKY']

    n_sa = len(sa_aktif)
    print(f'     → SA aktif ({n_sa}): {sorted(sa_aktif)}')

    rows = []
    for _, r in df_tgt.iterrows():
        cpus_reg = r['cpus_total']    - r['cpus_kha']
        rev_reg  = r['revenue_total'] - r['revenue_kha']
        ltr_reg  = r['liter_total']   - r['liter_kha']

        for sa in sa_aktif:
            rows.append({
                'tahun': int(r['tahun']), 'bulan': int(r['bulan']),
                'bulan_nama': r['bulan_nama'], 'sa': sa,
                'target_cpus':    round(cpus_reg / n_sa, 1),
                'target_revenue': round(rev_reg  / n_sa, 0),
                'target_liter':   round(ltr_reg  / n_sa, 1),
                'tipe': 'GR',
            })
        rows.append({
            'tahun': int(r['tahun']), 'bulan': int(r['bulan']),
            'bulan_nama': r['bulan_nama'], 'sa': 'KHA',
            'target_cpus':    r['cpus_kha'],
            'target_revenue': r['revenue_kha'],
            'target_liter':   r['liter_kha'],
            'tipe': 'TMS',
        })
        rows.append({
            'tahun': int(r['tahun']), 'bulan': int(r['bulan']),
            'bulan_nama': r['bulan_nama'], 'sa': 'TOTAL',
            'target_cpus':    r['cpus_total'],
            'target_revenue': r['revenue_total'],
            'target_liter':   r['liter_total'],
            'tipe': 'ALL',
        })
    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════
# 9. SQLITE — SMART UPDATE
# ════════════════════════════════════════════════════════════

def get_months_in_db(conn, table: str, date_col: str) -> set:
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT
                CAST(strftime('%Y',[{date_col}]) AS INTEGER),
                CAST(strftime('%m',[{date_col}]) AS INTEGER)
            FROM [{table}]
            WHERE [{date_col}] IS NOT NULL
        """).fetchall()
        return {(y,m) for y,m in rows}
    except:
        return set()

def get_months_in_df(df: pd.DataFrame, date_col: str) -> set:
    col = pd.to_datetime(df[date_col], errors='coerce').dropna()
    return {(d.year, d.month) for d in col}

def replace_months(conn, table: str, df: pd.DataFrame,
                   date_col: str, months: set) -> int:
    if not months:
        return 0

    # Buat tabel kalau belum ada
    exists = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
    ).fetchone()
    if not exists:
        df.head(0).to_sql(table, conn, if_exists='replace', index=False)
        conn.commit()

    for year, month in months:
        conn.execute(f"""
            DELETE FROM [{table}]
            WHERE CAST(strftime('%Y',[{date_col}]) AS INTEGER)={year}
            AND   CAST(strftime('%m',[{date_col}]) AS INTEGER)={month}
        """)

    dates = pd.to_datetime(df[date_col], errors='coerce')
    mask  = dates.apply(lambda d: (d.year,d.month) in months if pd.notna(d) else False)
    inserted = int(mask.sum())
    if inserted > 0:
        df[mask].to_sql(table, conn, if_exists='append', index=False)
    conn.commit()
    return inserted

def append_new_months(conn, table: str, df: pd.DataFrame,
                      date_col: str) -> int:
    existing   = get_months_in_db(conn, table, date_col)
    new_months = get_months_in_df(df, date_col) - existing
    if not new_months:
        return 0
    return replace_months(conn, table, df, date_col, new_months)


# ════════════════════════════════════════════════════════════
# 10. BUILD DAILY KPI TABLE
# ════════════════════════════════════════════════════════════

def build_daily_kpi(db_path: str):
    """
    Pre-agregasi per hari per SA → tabel daily_kpi.
    Lebih cepat dibaca AI Agent daripada query rekapbulanan langsung.
    Counter ditandai is_counter=1 agar AI tahu bukan SA.
    """
    conn = sqlite3.connect(db_path)

    conn.execute("DROP TABLE IF EXISTS daily_kpi")
    conn.execute("""
        CREATE TABLE daily_kpi AS
        SELECT
            tanggal,
            sa,
            is_counter,
            COUNT(DISTINCT no_wo)                           AS unit_entry,
            COUNT(DISTINCT CASE
                WHEN kategori IN ('SBE','GRP','LUB','TCARE')
                THEN no_wo END)                             AS cpus,
            ROUND(SUM(CASE
                WHEN is_counter=1 THEN total_revenue
                ELSE invoice END), 0)                       AS revenue,
            ROUND(SUM(COALESCE(jasa,   0)), 0)              AS jasa,
            ROUND(SUM(COALESCE(tgp,    0)), 0)              AS tgp,
            ROUND(SUM(COALESCE(adt,    0)), 0)              AS adt,
            ROUND(SUM(COALESCE(sublet, 0)), 0)              AS sublet,
            ROUND(SUM(
                COALESCE(adt,0) +
                COALESCE(oth,0) +
                COALESCE(sublet,0)
            ), 0)                                           AS upselling,
            ROUND(SUM(COALESCE(total_liter, 0)), 2)         AS total_liter
        FROM rekapbulanan
        WHERE sa IS NOT NULL AND TRIM(sa) <> ''
        GROUP BY tanggal, sa, is_counter
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dkpi_tgl ON daily_kpi(tanggal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dkpi_sa  ON daily_kpi(sa)")
    conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM daily_kpi").fetchone()[0]
    conn.close()
    print(f'   → daily_kpi: {n:,} baris')


# ════════════════════════════════════════════════════════════
# 11. SAVE TO DB
# ════════════════════════════════════════════════════════════

def save_to_db(db_path: str, df_um, df_inv, df_bp, df_rb,
               df_rs_full, df_target, baru_months: set):

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    print(f'\n   Simpan ke database...')

    # unitmasuk
    n = replace_months(conn, 'unitmasuk', df_um, 'tanggal',
                       get_months_in_df(df_um, 'tanggal'))
    print(f'   → unitmasuk    : {n:,} baris')

    # invoice (replace penuh — kecil)
    df_inv.to_sql('invoice', conn, if_exists='replace', index=False)
    print(f'   → invoice      : {len(df_inv):,} baris')

    # bufferparts (smart: baru=replace, cache=append baru)
    n_baru  = replace_months(conn,'bufferparts',df_bp,'tgl_faktur',baru_months)
    n_cache = append_new_months(conn,'bufferparts',df_bp,'tgl_faktur')
    print(f'   → bufferparts  : {n_baru:,} replace + {n_cache:,} append')

    # rekapbulanan
    n = replace_months(conn, 'rekapbulanan', df_rb, 'tanggal',
                       get_months_in_df(df_rb, 'tanggal'))
    print(f'   → rekapbulanan : {n:,} baris')

    # rs (replace penuh)
    if len(df_rs_full) > 0:
        df_rs_full.to_sql('rs', conn, if_exists='replace', index=False)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_rs_rangka ON rs(no_rangka)')
        conn.commit()
        print(f'   → rs           : {len(df_rs_full):,} unit')

    # target_bulanan (replace penuh)
    if len(df_target) > 0:
        df_target.to_sql('target_bulanan', conn, if_exists='replace', index=False)
        print(f'   → target       : {len(df_target):,} baris')

    # Total per tabel
    for tbl in ['unitmasuk','invoice','bufferparts','rekapbulanan','rs']:
        try:
            total = conn.execute(f'SELECT COUNT(*) FROM [{tbl}]').fetchone()[0]
            print(f'   DB {tbl:<15}: {total:,} total')
        except: pass

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    db_mb = Path(db_path).stat().st_size / 1024 / 1024
    print(f'   DB size: {db_mb:.1f} MB')


# ════════════════════════════════════════════════════════════
# 12. MAIN
# ════════════════════════════════════════════════════════════

def run(paths: dict = None):
    if paths is None:
        paths = PATHS

    t_start = time.time()
    print('\n' + '='*55)
    print('  ETL Nasmoco Tegal')
    print('='*55)

    Path(paths['output']).mkdir(parents=True, exist_ok=True)
    db_path = str(Path(paths['output']) / 'nasmoco.db')
    print(f'   DB Path: {db_path}')
    print('\n1/7  Load master...')
    oli_map, sublet_map = load_masters(paths['master'])
    print(f'     → {len(oli_map)} oli, {len(sublet_map)} sublet')

    print('2/7  Load RS...')
    df_rs = load_rs(paths['rs'])
    print(f'     → {len(df_rs):,} unit (untuk enrich)')

    print('3/7  Load invoice...')
    df_inv = load_invoice(paths['invoice'])
    print(f'     → {len(df_inv):,} WOs')

    print('4/7  Load unit masuk...')
    df_um_raw = load_unitmasuk(paths['unit_masuk'])
    print(f'     → {len(df_um_raw):,} baris, {df_um_raw["no_wo"].nunique():,} WOs')

    print('4b/7 Load target...')
    df_target = load_target_manual(paths['master'], df_um_raw)
    if len(df_target) > 0:
        print(f'     → {len(df_target)} rows target')
    else:
        print('     ⚠ Target tidak tersedia')

    print('5/7  Enrich unitmasuk...')
    df_um = enrich_unitmasuk(df_um_raw, sublet_map, df_inv, df_rs)
    own   = df_um['is_own'].sum()
    tcare = (df_um['tcare']=='TCARE').sum()
    print(f'     → {len(df_um):,} baris')
    print(f'     → Own: {own:,}, TCARE: {tcare:,}')

    print('6/7  Load parts...')
    df_bp = load_parts(paths['parts_baru'], paths['parts_cache'], oli_map, df_um)
    print(f'     → {len(df_bp):,} baris')
    print(f'     → Total liter: {df_bp["total_liter"].sum():.1f} L')

    print('7/7  Build rekapbulanan...')
    df_rb = build_rekapbulanan(df_inv, df_bp, df_um)
    print(f'     → {len(df_rb):,} baris')
    print(f'     → Jasa        : Rp {df_rb["jasa"].sum():,.0f}')
    print(f'     → TGP         : Rp {df_rb["tgp"].sum():,.0f}')
    print(f'     → Total Rev   : Rp {df_rb["total_revenue"].sum():,.0f}')

    # RS full (untuk DB)
    print('\n   Load RS full...')
    df_rs_full = load_rs_full(paths['rs'])
    print(f'   → {len(df_rs_full):,} unit')

    # Deteksi baru_months untuk bufferparts
    baru_months = set()
    for f in get_excel_files(paths['parts_baru']):
        try:
            df_tmp = parse_satu_parts(f)
            if len(df_tmp) > 0:
                baru_months |= get_months_in_df(df_tmp, 'tgl_faktur')
        except: pass

    # Simpan ke DB
    save_to_db(db_path, df_um, df_inv, df_bp, df_rb,
               df_rs_full, df_target, baru_months)

    # Build daily_kpi table
    build_daily_kpi(db_path)

    elapsed = time.time() - t_start
    print(f'\n✅ Selesai! Waktu ETL: {int(elapsed//60)}m {int(elapsed%60)}s')
    print('='*55 + '\n')


if __name__ == '__main__':
    run()