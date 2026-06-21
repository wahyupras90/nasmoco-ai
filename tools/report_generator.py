"""
tools/report_generator.py
Generate laporan HTML bulanan dari nasmoco.db
Output: Output\daily_report\laporan_YYYY-MM-DD.html
"""

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime


# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════

try:
    from config import DB_PATH
except ImportError:
    DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"

OUTPUT_DIR = Path(DB_PATH).parent.parent / "Output" / "daily_report"


# ════════════════════════════════════════
# HELPER FORMAT
# ════════════════════════════════════════

def fmt(v):
    v = int(v)
    if v >= 1_000_000_000: return f"Rp {v/1_000_000_000:.2f}M"
    if v >= 1_000_000:     return f"Rp {v/1_000_000:.1f}jt"
    return f"Rp {v:,}"

def pct(a, t):
    return round(a / t * 100) if t > 0 else 0

def bc(p):
    return '#22c55e' if p >= 90 else '#eab308' if p >= 70 else '#ef4444'

def bdg(p):
    c = bc(p)
    return f'<span class="bdg" style="background:{c}22;color:{c};border:1px solid {c}40">{p}%</span>'

def bar(p, c, h=5):
    w = min(p, 100)
    return (f'<div style="background:#e2e8f0;border-radius:3px;height:{h}px;overflow:hidden">'
            f'<div style="width:{w}%;height:100%;background:{c};border-radius:3px"></div></div>')

def mrow(lbl, a, t, rp=False):
    p  = pct(a, t); c = bc(p)
    af = fmt(a) if rp else f"{int(a):,}"
    tf = fmt(t) if rp else f"{int(t):,}"
    return f'''<div class="mr"><div class="ml">{lbl}</div>
    <div><div class="mn"><span class="ma">{af}</span><span class="mt">/ {tf}</span>{bdg(p)}</div>
    {bar(p, c, 6)}</div></div>'''


# ════════════════════════════════════════
# QUERY DATA
# ════════════════════════════════════════

def query_data(tahun: int, bulan: int) -> dict:
    periode    = f"{tahun}-{bulan:02d}"
    periode_yr = str(tahun)
    conn       = sqlite3.connect(DB_PATH)

    # KPI per SA bulan ini (tanpa counter)
    df_sa = pd.read_sql(f"""
        SELECT sa,
               SUM(unit_entry)              AS unit_entry,
               SUM(cpus)                    AS cpus,
               ROUND(SUM(revenue))          AS revenue,
               ROUND(SUM(jasa))             AS jasa,
               ROUND(SUM(tgp))              AS tgp,
               ROUND(SUM(adt))              AS adt,
               ROUND(SUM(sublet))           AS sublet,
               ROUND(SUM(upselling))        AS upselling,
               ROUND(SUM(total_liter), 1)   AS total_liter
        FROM daily_kpi
        WHERE strftime('%Y-%m', tanggal) = '{periode}'
          AND is_counter = 0
        GROUP BY sa
        ORDER BY revenue DESC
    """, conn)

    # Counter revenue bulan ini (terpisah, masuk ke total saja)
    df_ctr = pd.read_sql(f"""
        SELECT ROUND(SUM(revenue)) AS counter_revenue
        FROM daily_kpi
        WHERE strftime('%Y-%m', tanggal) = '{periode}'
          AND is_counter = 1
    """, conn)
    counter_rev = int(df_ctr['counter_revenue'].iloc[0] or 0)

    # Target bulan ini
    df_tgt = pd.read_sql(f"""
        SELECT sa, target_cpus, target_revenue,
               target_liter, tipe
        FROM target_bulanan
        WHERE tahun = {tahun} AND bulan = {bulan}
    """, conn) if _table_exists(conn, 'target_bulanan') else pd.DataFrame()

    # WIP per SA
    df_wip = pd.read_sql(f"""
        SELECT sa, COUNT(DISTINCT no_wo) AS wip
        FROM unitmasuk
        WHERE strftime('%Y-%m', tanggal) = '{periode}'
          AND kategori = 'CPUS'
          AND CAST(no_wo AS TEXT) NOT IN (
              SELECT CAST(no_wo AS TEXT) FROM invoice
              WHERE no_wo IS NOT NULL
          )
        GROUP BY sa
    """, conn)

    # TCARE per SA
    df_tcare = pd.read_sql(f"""
        SELECT sa, COUNT(DISTINCT no_wo) AS tcare_wo
        FROM unitmasuk
        WHERE strftime('%Y-%m', tanggal) = '{periode}'
          AND tcare = 'TCARE'
        GROUP BY sa
    """, conn)

    # YTM rekap bulanan
    df_ytm_bln = pd.read_sql(f"""
        SELECT strftime('%Y-%m', tanggal)     AS bulan,
               COUNT(DISTINCT no_wo)          AS wo,
               ROUND(SUM(total_revenue))      AS revenue,
               ROUND(SUM(jasa))               AS jasa,
               ROUND(SUM(tgp))                AS tgp,
               ROUND(SUM(adt))                AS adt,
               ROUND(SUM(sublet))             AS sublet
        FROM rekapbulanan
        WHERE strftime('%Y', tanggal) = '{periode_yr}'
   
        GROUP BY bulan ORDER BY bulan
    """, conn)

    # YTM rekap SA
    df_ytm_sa = pd.read_sql(f"""
        SELECT sa,
               SUM(unit_entry)            AS wo,
               SUM(cpus)                  AS cpus,
               ROUND(SUM(revenue))        AS revenue,
               ROUND(SUM(upselling))      AS upselling
        FROM daily_kpi
        WHERE strftime('%Y', tanggal) = '{periode_yr}'
          AND is_counter = 0
        GROUP BY sa ORDER BY revenue DESC
    """, conn)

    conn.close()

    # Merge SA data
    df = df_sa.merge(df_wip,   on='sa', how='left')
    df = df.merge(df_tcare,    on='sa', how='left')
    df = df.fillna(0)

    # Merge target (kalau ada)
    if len(df_tgt) > 0:
        df = df.merge(df_tgt[df_tgt['tipe'].isin(['GR','TMS'])][
            ['sa','target_cpus','target_revenue','target_liter']
        ], on='sa', how='left')
    else:
        df['target_cpus']    = (df['cpus']       * 1.10).round(0)
        df['target_revenue'] = (df['revenue']     * 1.10).round(0)
        df['target_liter']   = (df['total_liter'] * 1.10).round(1)

    df['target_ue']   = (df['target_cpus'] * 1.05).round(0)
    df['target_jasa'] = (df['jasa']        * 1.10).round(0)
    df['target_tgp']  = (df['tgp']         * 1.10).round(0)
    df['target_ups']  = (df['upselling']   * 1.10).round(0)

    # Metrik turunan
    df['rev_per_cpus'] = (df['revenue']   / df['cpus'].replace(0, 1)).round(0).astype(int)
    df['ups_per_cpus'] = (df['upselling'] / df['cpus'].replace(0, 1)).round(0).astype(int)

    # YTM SA metrik
    df_ytm_sa['rev_per_cpus'] = (
        df_ytm_sa['revenue'] / df_ytm_sa['cpus'].replace(0, 1)
    ).round(0).astype(int)

    return {
        'periode':     periode,
        'tahun':       tahun,
        'bulan':       bulan,
        'df':          df,
        'ytm_bln':     df_ytm_bln,
        'ytm_sa':      df_ytm_sa,
        'counter_rev': counter_rev,
    }


def _table_exists(conn, table: str) -> bool:
    r = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
    ).fetchone()
    return r is not None


# ════════════════════════════════════════
# BUILD HTML SECTIONS
# ════════════════════════════════════════

def build_h2h(df: pd.DataFrame) -> str:
    ranks = ['🥇','🥈','🥉','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣']
    rows  = ''
    df_s  = df.sort_values('revenue', ascending=False).reset_index(drop=True)
    for i, r in df_s.iterrows():
        p_rev  = pct(r['revenue'], r['target_revenue'])
        p_cpus = pct(r['cpus'],    r['target_cpus'])
        rpc    = int(r['rev_per_cpus'])
        c_rev  = bc(p_rev)
        rank   = ranks[i] if i < len(ranks) else f"{i+1}"
        rows += f'''
        <div class="rs-row">
          <div class="rs-left">
            <span class="rs-rank">{rank}</span>
            <span class="rs-sa">{r['sa']}</span>
          </div>
          <div class="rs-metrics">
            <div class="rs-metric">
              <div class="rs-lbl">Revenue</div>
              <div class="rs-val">{fmt(r['revenue'])}</div>
              {bar(p_rev, c_rev)}
              <div class="rs-pct" style="color:{c_rev}">{p_rev}%</div>
            </div>
            <div class="rs-metric">
              <div class="rs-lbl">CPUS</div>
              <div class="rs-val">{int(r['cpus']):,}</div>
              {bar(p_cpus, bc(p_cpus))}
              <div class="rs-pct" style="color:{bc(p_cpus)}">{p_cpus}%</div>
            </div>
            <div class="rs-metric">
              <div class="rs-lbl">Rev/CPUS</div>
              <div class="rs-val">{fmt(rpc)}</div>
              <div class="rs-pct" style="color:#64748b">per unit</div>
            </div>
            <div class="rs-metric">
              <div class="rs-lbl">WIP</div>
              <div class="rs-val" style="color:#ea580c">{int(r['wip'])}</div>
              <div class="rs-pct" style="color:#94a3b8">WO</div>
            </div>
          </div>
        </div>'''
    return rows


def build_sa_cards(df: pd.DataFrame) -> tuple:
    cards = ''
    for _, r in df.iterrows():
        ov  = pct(r['revenue'], r['target_revenue'])
        oc  = bc(ov)
        rpc = int(r['rev_per_cpus'])
        tgt_rpc = int(r['target_revenue'] / r['target_cpus']) if r['target_cpus'] > 0 else 1
        label = '🟢 On Track' if ov >= 90 else '🟡 Monitor' if ov >= 70 else '🔴 Perhatian'
        cards += f'''
        <div class="card">
          <div class="ch" style="border-left:4px solid {oc}">
            <div>
              <span class="cn">{r['sa']}</span>
              <span class="cbdg" style="background:{oc}22;color:{oc};border:1px solid {oc}40">{label}</span>
            </div>
            <div class="cr">{fmt(r['revenue'])}</div>
          </div>
          <div class="cm">
            <div class="cs">📋 Entry & Revenue</div>
            {mrow('Unit Entry',   r['unit_entry'], r['target_ue'])}
            {mrow('Revenue',      r['revenue'],    r['target_revenue'], True)}
            {mrow('TGP Sales',    r['tgp'],        r['target_tgp'],     True)}
            {mrow('Labor Sales',  r['jasa'],       r['target_jasa'],    True)}
            <div class="cs" style="margin-top:10px">🎯 CPUS & Upselling</div>
            {mrow('CPUS',                       r['cpus'],       r['target_cpus'])}
            {mrow('Upselling (ADT+OTH+Sublet)', r['upselling'], r['target_ups'], True)}
            {mrow('Rev/CPUS',                   rpc,             tgt_rpc,         True)}
            <div class="chips">
              <span class="chip cb">TCARE: {int(r['tcare_wo'])} WO</span>
              <span class="chip co">WIP: {int(r['wip'])} WO</span>
            </div>
          </div>
        </div>'''

    # TOTAL card (SA saja, tanpa counter)
    tot = {k: df[k].sum() for k in ['unit_entry','target_ue','revenue','target_revenue',
           'tgp','target_tgp','jasa','target_jasa','cpus','target_cpus',
           'upselling','target_ups','tcare_wo','wip']}
    tot_rpc     = int(tot['revenue'] / tot['cpus']) if tot['cpus'] > 0 else 0
    tot_tgt_rpc = int(tot['target_revenue'] / tot['target_cpus']) if tot['target_cpus'] > 0 else 1

    cards += f'''
    <div class="card total">
      <div class="ch" style="border-left:4px solid #1d4ed8;background:#eff6ff">
        <div><span class="cn" style="color:#1d4ed8">📊 TOTAL SA</span></div>
        <div class="cr" style="color:#1d4ed8">{fmt(tot['revenue'])}</div>
      </div>
      <div class="cm">
        <div class="cs">📋 Entry & Revenue</div>
        {mrow('Unit Entry',  tot['unit_entry'], tot['target_ue'])}
        {mrow('Revenue',     tot['revenue'],    tot['target_revenue'], True)}
        {mrow('TGP Sales',   tot['tgp'],        tot['target_tgp'],     True)}
        {mrow('Labor Sales', tot['jasa'],       tot['target_jasa'],    True)}
        <div class="cs" style="margin-top:10px">🎯 CPUS & Upselling</div>
        {mrow('CPUS',       tot['cpus'],       tot['target_cpus'])}
        {mrow('Upselling',  tot['upselling'],  tot['target_ups'],  True)}
        {mrow('Rev/CPUS',   tot_rpc,           tot_tgt_rpc,        True)}
        <div class="chips">
          <span class="chip cb">TCARE: {int(tot['tcare_wo'])} WO</span>
          <span class="chip co">WIP: {int(tot['wip'])} WO</span>
        </div>
      </div>
    </div>'''
    return cards, tot, tot_rpc, tot_tgt_rpc


def build_ytm_tables(ytm_bln: pd.DataFrame,
                     ytm_sa:  pd.DataFrame) -> tuple:
    bln_rows = ''
    for _, r in ytm_bln.iterrows():
        bln_rows += (f'<tr><td class="tl">{r["bulan"]}</td>'
                     f'<td>{int(r["wo"]):,}</td>'
                     f'<td>{fmt(r["revenue"])}</td>'
                     f'<td>{fmt(r["jasa"])}</td>'
                     f'<td>{fmt(r["tgp"])}</td>'
                     f'<td>{fmt(r["adt"])}</td>'
                     f'<td>{fmt(r["sublet"])}</td></tr>')

    sa_rows = ''
    for _, r in ytm_sa.iterrows():
        sa_rows += (f'<tr><td class="tl">{r["sa"]}</td>'
                    f'<td>{int(r["wo"]):,}</td>'
                    f'<td>{int(r["cpus"]):,}</td>'
                    f'<td>{fmt(r["revenue"])}</td>'
                    f'<td>{fmt(r["rev_per_cpus"])}</td>'
                    f'<td>{fmt(r["upselling"])}</td></tr>')

    ytm_tot     = ytm_sa.agg({'wo':'sum','cpus':'sum','revenue':'sum','upselling':'sum'})
    ytm_tot_rpc = int(ytm_tot['revenue'] / ytm_tot['cpus']) if ytm_tot['cpus'] > 0 else 0
    ytm_ups_rpc = int(ytm_tot['upselling'] / ytm_tot['cpus']) if ytm_tot['cpus'] > 0 else 0

    sa_rows += (f'<tr class="ttr"><td class="tl">TOTAL</td>'
                f'<td>{int(ytm_tot["wo"]):,}</td>'
                f'<td>{int(ytm_tot["cpus"]):,}</td>'
                f'<td>{fmt(ytm_tot["revenue"])}</td>'
                f'<td>{fmt(ytm_tot_rpc)}</td>'
                f'<td>{fmt(ytm_tot["upselling"])}</td></tr>')

    return bln_rows, sa_rows, ytm_tot, ytm_tot_rpc, ytm_ups_rpc


# ════════════════════════════════════════
# GENERATE HTML
# ════════════════════════════════════════

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f4f8;color:#1a2332;font-size:13px}
.hdr{background:linear-gradient(135deg,#1e3a5f,#1d4ed8);color:#fff;padding:14px;text-align:center}
.hdr h1{font-size:17px;font-weight:800}.hdr p{font-size:11px;opacity:.8;margin-top:3px}
.tabs{position:sticky;top:0;z-index:99;background:#1e3a5f;display:flex;border-bottom:1px solid #2d4a6e}
.tab{flex:1;padding:12px 6px;text-align:center;font-size:12px;font-weight:700;border:none;cursor:pointer;color:rgba(255,255,255,.55);background:transparent;border-bottom:3px solid transparent;transition:.15s}
.tab.on{color:#fff;border-bottom:3px solid #60a5fa;background:rgba(255,255,255,.05)}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;padding:12px;max-width:540px;margin:0 auto}
.kpi{background:#fff;border-radius:10px;padding:11px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kl{font-size:10px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.kv{font-size:17px;font-weight:800;margin:3px 0 2px}.ks{font-size:10px;color:#94a3b8}
.rsec{padding:0 12px 4px;max-width:540px;margin:0 auto}
.sec-t{font-size:11px;font-weight:800;color:#1e3a5f;text-transform:uppercase;letter-spacing:.5px;padding:10px 0 6px;display:flex;align-items:center;gap:6px}
.rs-box{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden;margin-bottom:10px}
.rs-row{display:flex;align-items:center;padding:10px 12px;border-bottom:1px solid #f1f5f9;gap:10px}
.rs-row:last-child{border-bottom:none}.rs-row:hover{background:#f8faff}
.rs-left{display:flex;flex-direction:column;align-items:center;min-width:38px}
.rs-rank{font-size:16px}.rs-sa{font-size:13px;font-weight:800;color:#1e3a5f}
.rs-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;flex:1}
.rs-metric{text-align:center}
.rs-lbl{font-size:9px;color:#94a3b8;font-weight:700;text-transform:uppercase;letter-spacing:.2px}
.rs-val{font-size:12px;font-weight:800;color:#1e293b;margin:2px 0}
.rs-pct{font-size:10px;font-weight:700;margin-top:2px}
.wrap{padding:0 12px 16px;max-width:540px;margin:0 auto;display:flex;flex-direction:column;gap:10px}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden}
.ch{padding:12px 14px;display:flex;justify-content:space-between;align-items:center;background:#fafbfc}
.cn{font-size:17px;font-weight:800;color:#1e3a5f}
.cbdg{font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;display:inline-block;margin-top:4px}
.cr{font-size:15px;font-weight:800;text-align:right}
.cm{padding:12px 14px}.cs{font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.mr{margin-bottom:10px}.ml{font-size:11px;color:#475569;font-weight:600;margin-bottom:3px}
.mn{display:flex;align-items:center;gap:6px;margin-bottom:3px;flex-wrap:wrap}
.ma{font-size:14px;font-weight:800;color:#1e293b}.mt{font-size:11px;color:#94a3b8}
.bdg{font-size:11px;font-weight:700;padding:2px 7px;border-radius:20px}
.chips{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.chip{font-size:11px;font-weight:700;padding:4px 10px;border-radius:20px}
.cb{background:#dbeafe;color:#1d4ed8}.co{background:#ffedd5;color:#c2410c}
.total .ch{background:#eff6ff}
.ytm-wrap{padding:0 12px 16px;max-width:540px;margin:0 auto}
.tbl-box{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow-x:auto;margin-bottom:12px}
table{width:100%;border-collapse:collapse;white-space:nowrap;font-size:12px}
thead th{background:#1e3a5f;color:#fff;padding:9px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;text-align:center}
thead th.thl{text-align:left}
td{padding:8px 10px;border-bottom:1px solid #f1f5f9;text-align:center}
td.tl{text-align:left;font-weight:700;color:#1e3a5f;background:#f8fafc}
tr:hover td{background:#f0f7ff}tr:hover td.tl{background:#dbeafe}
tr.ttr td{background:#1e3a5f!important;color:#fff!important;font-weight:800}
tr.ttr td.tl{background:#162d4a!important;color:#fff!important}
.hidden{display:none!important}
.footer{text-align:center;padding:12px;font-size:10px;color:#94a3b8}
"""

BULAN_ID = {1:'Januari',2:'Februari',3:'Maret',4:'April',5:'Mei',6:'Juni',
            7:'Juli',8:'Agustus',9:'September',10:'Oktober',11:'November',12:'Desember'}

def generate_html(data: dict) -> str:
    df          = data['df']
    ytm_bln     = data['ytm_bln']
    ytm_sa      = data['ytm_sa']
    tahun       = data['tahun']
    bulan       = data['bulan']
    counter_rev = data['counter_rev']

    h2h_rows                         = build_h2h(df)
    cards, tot, tot_rpc, tot_tgt_rpc = build_sa_cards(df)
    bln_rows, sa_rows, ytm_tot, ytm_tot_rpc, ytm_ups_rpc = build_ytm_tables(ytm_bln, ytm_sa)

    # Revenue total outlet = SA + Counter
    total_rev_outlet = tot['revenue'] + counter_rev
    label_bulan      = f"{BULAN_ID.get(bulan, bulan)} {tahun}"

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Performa {label_bulan} — Nasmoco Tegal</title>
<style>{CSS}</style>
</head>
<body>

<div class="hdr">
  <h1>📊 PERFORMA NASMOCO TEGAL</h1>
  <p>{label_bulan}</p>
</div>

<div class="tabs">
  <button class="tab on" onclick="show(1)">📅 Bulan Ini</button>
  <button class="tab"    onclick="show(2)">📈 YTM</button>
</div>

<!-- TAB 1: BULAN INI -->
<div id="t1">
  <div class="kpis">
    <div class="kpi">
      <div class="kl">Revenue</div>
      <div class="kv" style="color:#7c3aed">{fmt(total_rev_outlet)}</div>
      <div class="ks">{pct(total_rev_outlet, tot['target_revenue'])}% target</div>
    </div>
    <div class="kpi">
      <div class="kl">CPUS</div>
      <div class="kv" style="color:#1d4ed8">{int(tot['cpus']):,}</div>
      <div class="ks">{pct(tot['cpus'],tot['target_cpus'])}% target</div>
    </div>
    <div class="kpi">
      <div class="kl">Rev/CPUS</div>
      <div class="kv" style="color:#059669">{fmt(tot_rpc)}</div>
      <div class="ks">Target {fmt(tot_tgt_rpc)}</div>
    </div>
    <div class="kpi">
      <div class="kl">WIP</div>
      <div class="kv" style="color:#ea580c">{int(tot['wip']):,}</div>
      <div class="ks">Belum diinvoice</div>
    </div>
  </div>

  <div class="rsec">
    <div class="sec-t">⚡ Head-to-Head SA</div>
    <div class="rs-box">{h2h_rows}</div>
  </div>

  <div class="rsec"><div class="sec-t">📋 Detail per SA</div></div>
  <div class="wrap">{cards}</div>
</div>

<!-- TAB 2: YTM -->
<div id="t2" class="hidden">
  <div class="ytm-wrap">
    <div class="kpis" style="padding:12px 0">
      <div class="kpi">
        <div class="kl">Revenue YTM</div>
        <div class="kv" style="color:#7c3aed">{fmt(ytm_tot['revenue'])}</div>
        <div class="ks">{len(ytm_bln)} bulan</div>
      </div>
      <div class="kpi">
        <div class="kl">CPUS YTM</div>
        <div class="kv" style="color:#1d4ed8">{int(ytm_tot['cpus']):,}</div>
        <div class="ks">WO: {int(ytm_tot['wo']):,}</div>
      </div>
      <div class="kpi">
        <div class="kl">Rev/CPUS YTM</div>
        <div class="kv" style="color:#059669">{fmt(ytm_tot_rpc)}</div>
        <div class="ks">rata-rata</div>
      </div>
      <div class="kpi">
        <div class="kl">Upselling YTM</div>
        <div class="kv" style="color:#ea580c">{fmt(ytm_tot['upselling'])}</div>
        <div class="ks">{fmt(ytm_ups_rpc)} /CPUS</div>
      </div>
    </div>

    <div class="sec-t" style="padding:4px 0 8px;font-size:11px;font-weight:800;
         color:#1e3a5f;text-transform:uppercase;letter-spacing:.5px">📅 Rekap Bulanan</div>
    <div class="tbl-box"><table>
      <thead><tr>
        <th class="thl">Bulan</th><th>WO</th><th>Revenue</th>
        <th>Jasa</th><th>TGP</th><th>ADT</th><th>Sublet</th>
      </tr></thead>
      <tbody>{bln_rows}</tbody>
    </table></div>

    <div class="sec-t" style="padding:4px 0 8px;font-size:11px;font-weight:800;
         color:#1e3a5f;text-transform:uppercase;letter-spacing:.5px">👤 Rekap SA YTM</div>
    <div class="tbl-box"><table>
      <thead><tr>
        <th class="thl">SA</th><th>WO</th><th>CPUS</th>
        <th>Revenue</th><th>Rev/CPUS</th><th>Upselling</th>
      </tr></thead>
      <tbody>{sa_rows}</tbody>
    </table></div>
  </div>
</div>

<div class="footer">Nasmoco Tegal &nbsp;·&nbsp; AI Agent Report &nbsp;·&nbsp; {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>

<script>
function show(n){{
  document.getElementById('t1').classList.toggle('hidden', n !== 1);
  document.getElementById('t2').classList.toggle('hidden', n !== 2);
  document.querySelectorAll('.tab').forEach(function(t,i){{
    t.classList.toggle('on', i === n-1);
  }});
}}
</script>
</body>
</html>"""


# ════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════

def buat_laporan(tahun: int = None, bulan: int = None) -> str:
    today = datetime.today()
    if not tahun: tahun = today.year
    if not bulan: bulan = today.month

    print(f"  Mengambil data {tahun}-{bulan:02d}...")
    data = query_data(tahun, bulan)

    print(f"  Generate HTML...")
    html = generate_html(data)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"laporan_{today.strftime('%Y-%m-%d')}.html"
    filepath = OUTPUT_DIR / filename

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"  ✅ Laporan tersimpan: {filepath}")
    return str(filepath)


if __name__ == '__main__':
    buat_laporan()