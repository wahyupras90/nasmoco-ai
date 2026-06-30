# -*- coding: utf-8 -*-
"""
generate_tcare_chart.py
Generate file HTML interaktif (potensi vs realisasi TCARE) langsung dari nasmoco.db.
Filter: Own/Berkah (checkbox) + Tcare Type: T-CARE / T-CARE LITE / T-CARE LITE+ / RANGGA (checkbox).

Jalankan: python generate_tcare_chart.py
Output  : D:/AI_nasmoco/outputs/tcare_chart.html  (bisa diubah lewat OUTPUT_PATH)
"""

import sqlite3
import json
from pathlib import Path

DB_PATH = r"D:\AI_nasmoco\db\nasmoco.db"
OUTPUT_PATH = r"D:\AI_nasmoco\Output\tcare_chart.html"
BULAN_MULAI = "2025-01"
BULAN_AKHIR = "2026-12"

TCARE_TYPES = ["T-CARE", "T-CARE LITE", "T-CARE LITE+", "RANGGA"]
TYPE_COLOR = {
    "T-CARE": "#2563eb",
    "T-CARE LITE": "#059669",
    "T-CARE LITE+": "#d97706",
    "RANGGA": "#7c3aed",
}

MONTH_LABELS = ["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Agu","Sep","Okt","Nov","Des"]


def fetch_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT bulan_jadwal,
               COALESCE(tu.tcare_type, 'T-CARE') as tcare_type,
               ts.dealer_kategori,
               COUNT(*) as cnt
        FROM tcare_schedule ts
        JOIN tcare_unit tu ON ts.no_rangka = tu.no_rangka
        WHERE bulan_jadwal BETWEEN ? AND ?
        GROUP BY bulan_jadwal, tcare_type, ts.dealer_kategori
        ORDER BY bulan_jadwal
    """, (BULAN_MULAI, BULAN_AKHIR))
    potensi_raw = cur.fetchall()

    cur.execute("""
        SELECT bulan_realisasi,
               COALESCE(tu.tcare_type, 'T-CARE') as tcare_type,
               ts.dealer_kategori,
               COUNT(*) as cnt
        FROM tcare_schedule ts
        JOIN tcare_unit tu ON ts.no_rangka = tu.no_rangka
        WHERE ts.status != 'pending'
          AND bulan_realisasi BETWEEN ? AND ?
        GROUP BY bulan_realisasi, tcare_type, ts.dealer_kategori
        ORDER BY bulan_realisasi
    """, (BULAN_MULAI, BULAN_AKHIR))
    realisasi_raw = cur.fetchall()

    conn.close()
    return potensi_raw, realisasi_raw


def build_dataset(potensi_raw, realisasi_raw):
    """
    Bentuk struktur:
    {
      "2025-01": {
        "OWN": {"T-CARE": {"po": x, "ro": y}, "T-CARE LITE": {...}, ...},
        "BERKAH": {...}
      },
      ...
    }
    """
    months = sorted(set([r[0] for r in potensi_raw] + [r[0] for r in realisasi_raw]))
    data = {}
    for m in months:
        data[m] = {
            "OWN": {t: {"po": 0, "ro": 0} for t in TCARE_TYPES},
            "BERKAH": {t: {"po": 0, "ro": 0} for t in TCARE_TYPES},
        }

    for bulan, ttype, kategori, cnt in potensi_raw:
        if ttype not in TCARE_TYPES or kategori not in ("OWN", "BERKAH"):
            continue
        data[bulan][kategori][ttype]["po"] += cnt

    for bulan, ttype, kategori, cnt in realisasi_raw:
        if ttype not in TCARE_TYPES or kategori not in ("OWN", "BERKAH"):
            continue
        if bulan not in data:
            data[bulan] = {
                "OWN": {t: {"po": 0, "ro": 0} for t in TCARE_TYPES},
                "BERKAH": {t: {"po": 0, "ro": 0} for t in TCARE_TYPES},
            }
        data[bulan][kategori][ttype]["ro"] += cnt

    return data


def render_html(data: dict) -> str:
    data_json = json.dumps(data, ensure_ascii=False)
    types_json = json.dumps(TCARE_TYPES)
    colors_json = json.dumps(TYPE_COLOR)
    months_json = json.dumps(MONTH_LABELS)

    html = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TCARE — Potensi & Realisasi</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f5;color:#1a1a2e;min-height:100vh;padding:24px 20px;}
  .page{max-width:1000px;margin:0 auto;}
  .header{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px;}
  .brand{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#e53010;margin-bottom:4px;}
  .title{font-size:22px;font-weight:700;line-height:1.2;}
  .subtitle{font-size:13px;color:#6b7280;margin-top:3px;}
  .period-select{display:flex;align-items:center;gap:6px;font-size:13px;color:#6b7280;}
  .period-select select{font-size:13px;font-family:inherit;padding:6px 12px;border-radius:8px;border:1px solid #d1d5db;background:#fff;color:#1a1a2e;cursor:pointer;outline:none;}
  .period-select select:focus{border-color:#e53010;}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px;}
  @media (max-width:680px){.stats{grid-template-columns:repeat(2,1fr);}}
  .stat{background:#fff;border-radius:10px;padding:12px 16px;border-left:3px solid #e5e7eb;}
  .stat.own{border-left-color:#2563eb;}
  .stat.berkah{border-left-color:#059669;}
  .stat.conv-o{border-left-color:#7c3aed;}
  .stat.conv-b{border-left-color:#d97706;}
  .stat-label{font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px;}
  .stat-val{font-size:24px;font-weight:700;line-height:1.1;}
  .stat-sub{font-size:11px;color:#9ca3af;margin-top:3px;}
  .chart-card{background:#fff;border-radius:12px;padding:20px 20px 16px;margin-bottom:14px;}
  .controls-group{margin-bottom:12px;}
  .controls-title{font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;}
  .controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
  .cb-label{display:flex;align-items:center;gap:7px;font-size:13px;color:#374151;cursor:pointer;user-select:none;padding:5px 10px;border-radius:6px;border:1px solid #e5e7eb;transition:background .15s;}
  .cb-label:hover{background:#f9fafb;}
  .cb-label input{display:none;}
  .cb-swatch{width:12px;height:12px;border-radius:3px;flex-shrink:0;}
  .cb-swatch.dashed{background:transparent!important;border-top:2.5px dashed;border-radius:0;height:0;width:16px;margin:6px 0;}
  .cb-label input:not(:checked) ~ .cb-swatch{opacity:.25;}
  .cb-label input:not(:checked) ~ span{color:#9ca3af;}
  .chart-wrap{position:relative;width:100%;height:320px;}
  .note{font-size:11px;color:#9ca3af;margin-top:10px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;}
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <div>
      <div class="brand">Nasmoco Tegal · Workshop Analytics</div>
      <div class="title">TCARE — Potensi &amp; Realisasi</div>
      <div class="subtitle">Potensi per bulan jadwal · Realisasi per bulan kedatangan aktual</div>
    </div>
    <div class="period-select">
      <span>Periode</span>
      <select id="sel-range">
        <option value="2025">2025</option>
        <option value="2026" selected>2026</option>
        <option value="all">Semua (2025–2026)</option>
      </select>
    </div>
  </div>

  <div class="stats" id="stat-row"></div>

  <div class="chart-card">

    <div class="controls-group">
      <div class="controls-title">Kategori Dealer</div>
      <div class="controls" id="ctl-kategori"></div>
    </div>

    <div class="controls-group">
      <div class="controls-title">Tipe T-CARE</div>
      <div class="controls" id="ctl-type"></div>
    </div>

    <div class="chart-wrap"><canvas id="tcareChart"></canvas></div>

    <div class="note">
      <span>Potensi: <code>bulan_jadwal</code> · Realisasi: <code>bulan_realisasi</code> (kedatangan aktual)</span>
      <span>Sumber: tcare_schedule + tcare_unit</span>
    </div>
  </div>

</div>

<script>
const RAW = __DATA_JSON__;
const TCARE_TYPES = __TYPES_JSON__;
const TYPE_COLOR = __COLORS_JSON__;
const MN = __MONTHS_JSON__;

const KATEGORI = ["OWN", "BERKAH"];
const KATEGORI_COLOR = {OWN: "#2563eb", BERKAH: "#059669"};

function label(b){ const[y,m]=b.split("-"); return MN[+m-1]+" "+y.slice(2); }

// state
let activeKategori = new Set(KATEGORI);
let activeTypes = new Set(TCARE_TYPES);

function buildControls(){
  const ck = document.getElementById("ctl-kategori");
  ck.innerHTML = KATEGORI.map(k => `
    <label class="cb-label">
      <input type="checkbox" data-kategori="${k}" checked>
      <span class="cb-swatch" style="background:${KATEGORI_COLOR[k]};"></span>
      <span>${k === "OWN" ? "Own" : "Berkah"}</span>
    </label>`).join("");

  const ct = document.getElementById("ctl-type");
  ct.innerHTML = TCARE_TYPES.map(t => `
    <label class="cb-label">
      <input type="checkbox" data-type="${t}" checked>
      <span class="cb-swatch" style="background:${TYPE_COLOR[t]};"></span>
      <span>${t}</span>
    </label>`).join("");

  ck.querySelectorAll("input").forEach(inp=>{
    inp.addEventListener("change", ()=>{
      const k = inp.dataset.kategori;
      if(inp.checked) activeKategori.add(k); else activeKategori.delete(k);
      render();
    });
  });
  ct.querySelectorAll("input").forEach(inp=>{
    inp.addEventListener("change", ()=>{
      const t = inp.dataset.type;
      if(inp.checked) activeTypes.add(t); else activeTypes.delete(t);
      render();
    });
  });
}

function getMonths(range){
  const all = Object.keys(RAW).sort();
  if(range==="all") return all;
  return all.filter(m=>m.startsWith(range));
}

function aggregate(months){
  // sum po/ro per bulan, across selected kategori & types
  return months.map(m=>{
    let po=0, ro=0;
    KATEGORI.forEach(k=>{
      if(!activeKategori.has(k)) return;
      TCARE_TYPES.forEach(t=>{
        if(!activeTypes.has(t)) return;
        const cell = RAW[m] && RAW[m][k] && RAW[m][k][t];
        if(cell){ po += cell.po; ro += cell.ro; }
      });
    });
    return {bulan:m, po, ro};
  });
}

// breakdown per kategori (untuk dual-line/bar by Own/Berkah) — dipakai untuk chart utama
function aggregateByKategori(months){
  const result = {};
  KATEGORI.forEach(k=>{
    result[k] = months.map(m=>{
      let po=0, ro=0;
      if(activeKategori.has(k)){
        TCARE_TYPES.forEach(t=>{
          if(!activeTypes.has(t)) return;
          const cell = RAW[m] && RAW[m][k] && RAW[m][k][t];
          if(cell){ po += cell.po; ro += cell.ro; }
        });
      }
      return {bulan:m, po, ro};
    });
  });
  return result;
}

function updateStats(months){
  const rows = aggregate(months);
  const sp = rows.reduce((s,d)=>s+d.po,0);
  const sr = rows.reduce((s,d)=>s+d.ro,0);

  const byK = aggregateByKategori(months);
  const spo = byK.OWN.reduce((s,d)=>s+d.po,0);
  const sro = byK.OWN.reduce((s,d)=>s+d.ro,0);
  const spb = byK.BERKAH.reduce((s,d)=>s+d.po,0);
  const srb = byK.BERKAH.reduce((s,d)=>s+d.ro,0);

  const co = spo>0 ? Math.round(sro/spo*100) : null;
  const cb = spb>0 ? Math.round(srb/spb*100) : null;

  document.getElementById("stat-row").innerHTML = `
    <div class="stat own">
      <div class="stat-label">Potensi Own</div>
      <div class="stat-val">${spo.toLocaleString("id")}</div>
      <div class="stat-sub">Realisasi ${sro.toLocaleString("id")} unit</div>
    </div>
    <div class="stat berkah">
      <div class="stat-label">Potensi Berkah</div>
      <div class="stat-val">${spb.toLocaleString("id")}</div>
      <div class="stat-sub">Realisasi ${srb.toLocaleString("id")} unit</div>
    </div>
    <div class="stat conv-o">
      <div class="stat-label">Konversi Own</div>
      <div class="stat-val">${co!==null?co+"%":"—"}</div>
      <div class="stat-sub">Realisasi vs potensi</div>
    </div>
    <div class="stat conv-b">
      <div class="stat-label">Konversi Berkah</div>
      <div class="stat-val">${cb!==null?cb+"%":"—"}</div>
      <div class="stat-sub">Realisasi vs potensi</div>
    </div>`;
}

let chart;
function buildChart(months){
  const labels = months.map(label);
  const byK = aggregateByKategori(months);
  const datasets = [];

  KATEGORI.forEach(k=>{
    if(!activeKategori.has(k)) return;
    const color = KATEGORI_COLOR[k];
    const rows = byK[k];
    datasets.push({
      label: `Potensi ${k==="OWN"?"Own":"Berkah"}`,
      data: rows.map(d=>d.po), type:"bar",
      backgroundColor: color+"a6", borderColor: color,
      borderWidth:1, borderRadius:4, order:2
    });
    datasets.push({
      label: `Realisasi ${k==="OWN"?"Own":"Berkah"}`,
      data: rows.map(d=>d.ro||null), type:"line",
      borderColor: color, borderWidth:2.5, borderDash:[6,4],
      pointRadius:4, pointBackgroundColor:"#fff",
      pointBorderColor: color, pointBorderWidth:2,
      tension:.3, fill:false, order:1, spanGaps:false
    });
  });

  if(chart) chart.destroy();
  chart = new Chart(document.getElementById("tcareChart"), {
    type:"bar",
    data:{labels, datasets},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display:true, position:"top", labels:{boxWidth:12, font:{size:11}}},
        tooltip:{
          backgroundColor:"#1a1a2e", titleFont:{size:12}, bodyFont:{size:12}, padding:10,
          callbacks:{ label: ctx => {
            const v = ctx.parsed.y;
            return v!==null ? ` ${ctx.dataset.label}: ${v} unit` : ` ${ctx.dataset.label}: —`;
          }}
        }
      },
      scales:{
        x:{ grid:{display:false}, ticks:{font:{size:11},color:"#9ca3af",autoSkip:false,maxRotation:45} },
        y:{ grid:{color:"#f3f4f6"}, border:{dash:[4,4]}, ticks:{font:{size:11},color:"#9ca3af"},
            title:{display:true,text:"Jumlah unit",font:{size:11},color:"#9ca3af"} }
      }
    }
  });
}

function render(){
  const range = document.getElementById("sel-range").value;
  const months = getMonths(range);
  updateStats(months);
  buildChart(months);
}

buildControls();
document.getElementById("sel-range").addEventListener("change", render);
render();
</script>
</body>
</html>
"""
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__TYPES_JSON__", types_json)
    html = html.replace("__COLORS_JSON__", colors_json)
    html = html.replace("__MONTHS_JSON__", months_json)
    return html


def main():
    potensi_raw, realisasi_raw = fetch_data()
    data = build_dataset(potensi_raw, realisasi_raw)
    html = render_html(data)

    out_path = Path(OUTPUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    print(f"HTML berhasil dibuat: {out_path}")
    print(f"Total bulan: {len(data)}")


if __name__ == "__main__":
    main()