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
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


# ════════════════════════════════════════
# ROUTING KEYWORDS (sama persis dengan main.py)
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

EXCEL_KEYWORDS = [
    "export excel", "export ke excel", "simpan excel",
    "simpan ke excel", "download excel", "ke excel",
    "export", "unduh excel", "unduh"
]

def need_report(text: str) -> bool:
    return any(w in text.lower() for w in LAPORAN_KEYWORDS)

def need_export(text: str) -> bool:
    return any(w in text.lower() for w in EXCEL_KEYWORDS)

def need_claude(text: str) -> bool:
    t = text.lower().strip()
    return t.startswith("claude ") or t.startswith("claude,")

def need_analysis(text: str) -> bool:
    return any(w in text.lower() for w in ANALYSIS_KEYWORDS)


# ════════════════════════════════════════
# SESSION STORE — _last_result per user
# ════════════════════════════════════════

import pandas as pd

_sessions: dict[str, pd.DataFrame] = {}  # session_id → last DataFrame

def get_last_result(session_id: str) -> pd.DataFrame:
    return _sessions.get(session_id, pd.DataFrame())

def set_last_result(session_id: str, df: pd.DataFrame):
    _sessions[session_id] = df.copy()


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

def capture_run(fn, *args, **kwargs) -> str:
    """Jalankan fn dan kembalikan semua output print() sebagai string."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    output = buf.getvalue().strip()

    # Sembunyikan baris MODEL= kalau bukan debug mode
    debug = kwargs.get('debug', False)
    if not debug:
        lines = [l for l in output.splitlines() if not l.startswith('MODEL=')]
        output = '\n'.join(lines).strip()

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

    # Inject last result milik session ini ke global sql_agent
    sql_agent._last_result = get_last_result(session_id)

    output = capture_run(sql_agent.run, pertanyaan, debug=debug)

    # Filter MODEL= juga di path sql_agent (capture_run sudah handle, tapi jaga-jaga)
    if not debug:
        lines = [l for l in output.splitlines() if not l.startswith('MODEL=')]
        output = '\n'.join(lines).strip()

    # Ambil kembali _last_result yang mungkin sudah diupdate
    set_last_result(session_id, sql_agent._last_result)

    return output


# ════════════════════════════════════════
# HANDLER LAPORAN
# ════════════════════════════════════════

def handle_laporan(pertanyaan: str) -> str:
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
        return f"✅ Laporan selesai:\n{filepath}\n\nBuka di browser atau kirim ke grup WA."

    except ImportError:
        return "⚠ report_generator.py tidak ditemukan di tools/"
    except Exception as e:
        return f"ERROR generate laporan: {e}"


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

        if need_report(pertanyaan):
            result = await loop.run_in_executor(None, handle_laporan, pertanyaan)

        elif need_claude(pertanyaan):
            q = re.sub(r'^claude[,\s]+', '', pertanyaan, flags=re.IGNORECASE).strip()
            from ai.investigator import run as investigator_run
            result = await loop.run_in_executor(
                None, lambda: capture_run(investigator_run, q, debug=debug)
            )

        elif need_analysis(pertanyaan):
            from ai.investigator import run as investigator_run
            result = await loop.run_in_executor(
                None, lambda: capture_run(investigator_run, pertanyaan, debug=debug)
            )

        else:
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

    # Export mode — kirim sinyal ke client untuk trigger download
    if isinstance(result, dict) and result.get("export"):
        return JSONResponse({
            "export": True,
            "filename": result["filename"],
            "rows": result["rows"],
            "session_id": session_id,
        })

    return JSONResponse({"response": result or "(Tidak ada output)"})


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


# ════════════════════════════════════════
# CHAT UI HTML
# ════════════════════════════════════════

CHAT_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nasmoco AI</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3350;
    --accent: #e8003d;
    --accent2: #ff6b35;
    --text: #e8eaf0;
    --text2: #8890aa;
    --user-bubble: #1e3a5f;
    --ai-bubble: #1a1d27;
    --mono: 'Courier New', monospace;
    --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }

  body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    height: 100dvh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }

  .logo {
    width: 32px; height: 32px;
    background: var(--accent);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 900; font-size: 14px; color: white;
    flex-shrink: 0;
  }

  header h1 {
    font-size: 15px; font-weight: 700;
    letter-spacing: 0.5px;
  }

  header span {
    font-size: 11px; color: var(--text2);
    display: block; margin-top: 1px;
  }

  .status-dot {
    width: 7px; height: 7px;
    background: #22c55e;
    border-radius: 50%;
    margin-left: auto;
    flex-shrink: 0;
    box-shadow: 0 0 6px #22c55e88;
  }

  /* ── Chat area ── */
  #chat {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    scroll-behavior: smooth;
  }

  /* scrollbar */
  #chat::-webkit-scrollbar { width: 4px; }
  #chat::-webkit-scrollbar-track { background: transparent; }
  #chat::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .msg {
    display: flex;
    flex-direction: column;
    max-width: 88%;
    animation: fadeIn 0.18s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .msg.user { align-self: flex-end; align-items: flex-end; }
  .msg.ai   { align-self: flex-start; align-items: flex-start; }

  .bubble {
    padding: 10px 14px;
    border-radius: 14px;
    font-size: 14px;
    line-height: 1.55;
    word-break: break-word;
    white-space: pre-wrap;
  }

  .msg.user .bubble {
    background: var(--user-bubble);
    border-bottom-right-radius: 4px;
    color: #d8e8ff;
  }

  .msg.ai .bubble {
    background: var(--ai-bubble);
    border: 1px solid var(--border);
    border-bottom-left-radius: 4px;
    font-family: var(--mono);
    font-size: 13px;
  }

  /* Highlight angka di response AI */
  .msg.ai .bubble .num { color: #7dd3fc; font-weight: 600; }

  .msg-time {
    font-size: 10px;
    color: var(--text2);
    margin-top: 4px;
    padding: 0 4px;
  }

  /* Typing indicator */
  .typing { display: flex; gap: 4px; padding: 12px 14px; }
  .typing span {
    width: 7px; height: 7px;
    background: var(--text2);
    border-radius: 50%;
    animation: bounce 1.2s infinite;
  }
  .typing span:nth-child(2) { animation-delay: .2s; }
  .typing span:nth-child(3) { animation-delay: .4s; }
  @keyframes bounce {
    0%,60%,100% { transform: translateY(0); }
    30%          { transform: translateY(-6px); }
  }

  /* ── Quick commands ── */
  #quick-cmds {
    display: flex;
    gap: 6px;
    padding: 8px 16px 0;
    overflow-x: auto;
    flex-shrink: 0;
  }
  #quick-cmds::-webkit-scrollbar { display: none; }

  .qcmd {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 5px 12px;
    font-size: 12px;
    color: var(--text2);
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.15s;
    flex-shrink: 0;
  }
  .qcmd:hover, .qcmd:active {
    background: var(--border);
    color: var(--text);
  }

  /* ── Input area ── */
  #input-area {
    padding: 10px 12px 14px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    display: flex;
    gap: 8px;
    align-items: flex-end;
    flex-shrink: 0;
  }

  #input {
    flex: 1;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 10px 14px;
    font-size: 14px;
    color: var(--text);
    font-family: var(--font);
    resize: none;
    outline: none;
    max-height: 120px;
    line-height: 1.4;
    transition: border-color 0.15s;
  }
  #input:focus { border-color: var(--accent); }
  #input::placeholder { color: var(--text2); }

  #send-btn {
    background: var(--accent);
    border: none;
    border-radius: 10px;
    width: 40px; height: 40px;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    transition: background 0.15s, transform 0.1s;
  }
  #send-btn:hover   { background: #c4002f; }
  #send-btn:active  { transform: scale(0.95); }
  #send-btn svg { width: 18px; height: 18px; fill: white; }

  /* ── Intro / empty state ── */
  #empty-state {
    margin: auto;
    text-align: center;
    color: var(--text2);
    padding: 24px;
  }
  #empty-state .big { font-size: 36px; margin-bottom: 8px; }
  #empty-state p { font-size: 13px; line-height: 1.6; }

  /* ── Download button ── */
  .dl-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-top: 8px;
    padding: 7px 14px;
    background: #16a34a;
    color: white;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-family: var(--font);
    cursor: pointer;
    text-decoration: none;
    transition: background 0.15s;
  }
  .dl-btn:hover { background: #15803d; }
  .dl-btn svg { width: 14px; height: 14px; fill: white; }

  /* ── Debug badge ── */
  #debug-toggle {
    font-size: 11px;
    color: var(--text2);
    cursor: pointer;
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-radius: 10px;
    user-select: none;
  }
  #debug-toggle.on { color: var(--accent2); border-color: var(--accent2); }
</style>
</head>
<body>

<header>
  <div class="logo">N</div>
  <div>
    <h1>Nasmoco AI</h1>
    <span>Analyst · Tegal</span>
  </div>
  <span id="debug-toggle" onclick="toggleDebug()">debug off</span>
  <div class="status-dot" title="Online"></div>
</header>

<div id="quick-cmds">
  <div class="qcmd" onclick="useCmd(this)">Revenue bulan ini</div>
  <div class="qcmd" onclick="useCmd(this)">CPUS bulan ini per SA</div>
  <div class="qcmd" onclick="useCmd(this)">Ranking SA bulan ini</div>
  <div class="qcmd" onclick="useCmd(this)">TCARE expired bulan ini</div>
  <div class="qcmd" onclick="useCmd(this)">WIP sekarang</div>
  <div class="qcmd" onclick="useCmd(this)">Buat laporan</div>
</div>

<div id="chat">
  <div id="empty-state">
    <div class="big">🔍</div>
    <p>Tanya apa saja tentang data bengkel.<br>
    Ketik atau pilih shortcut di atas.</p>
  </div>
</div>

<div id="input-area">
  <textarea id="input" rows="1" placeholder="Tanya sesuatu..." 
    onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
  <button id="send-btn" onclick="sendMsg()">
    <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
  </button>
</div>

<script>
  // ── Session ID unik per tab ──
  const SESSION_ID = 'sess_' + Math.random().toString(36).slice(2, 10);
  let debugMode = false;
  let isLoading = false;

  function toggleDebug() {
    debugMode = !debugMode;
    const el = document.getElementById('debug-toggle');
    el.textContent = debugMode ? 'debug on' : 'debug off';
    el.classList.toggle('on', debugMode);
  }

  function useCmd(el) {
    const inp = document.getElementById('input');
    inp.value = el.textContent;
    autoResize(inp);
    inp.focus();
  }

  function autoResize(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
  }

  function handleKey(e) {
    // Enter kirim, Shift+Enter newline
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMsg();
    }
  }

  function now() {
    return new Date().toLocaleTimeString('id-ID', {hour:'2-digit', minute:'2-digit'});
  }

  function addMsg(role, text) {
    const chat = document.getElementById('chat');
    const empty = document.getElementById('empty-state');
    if (empty) empty.remove();

    const div = document.createElement('div');
    div.className = `msg ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    if (role === 'ai') {
      // Highlight angka
      bubble.innerHTML = text.replace(/(\\b[\\d,]+(?:\\.\\d+)?\\b)/g, '<span class="num">$1</span>');
    } else {
      bubble.textContent = text;
    }

    const time = document.createElement('div');
    time.className = 'msg-time';
    time.textContent = now();

    div.appendChild(bubble);
    div.appendChild(time);
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
  }

  function addTyping() {
    const chat = document.getElementById('chat');
    const div = document.createElement('div');
    div.className = 'msg ai';
    div.id = 'typing-indicator';
    div.innerHTML = '<div class="bubble typing"><span></span><span></span><span></span></div>';
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }

  function removeTyping() {
    const t = document.getElementById('typing-indicator');
    if (t) t.remove();
  }

  async function sendMsg() {
    if (isLoading) return;
    const inp = document.getElementById('input');
    const text = inp.value.trim();
    if (!text) return;

    inp.value = '';
    inp.style.height = 'auto';
    addMsg('user', text);
    addTyping();
    isLoading = true;

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          session_id: SESSION_ID,
          debug: debugMode
        })
      });
      const data = await res.json();
      removeTyping();

      // ── Mode export: tampilkan tombol download ──
      if (data.export) {
        addExportMsg(data.filename, data.rows, data.session_id);
      } else {
        addMsg('ai', data.response);
      }

    } catch (err) {
      removeTyping();
      addMsg('ai', '⚠ Gagal menghubungi server: ' + err.message);
    } finally {
      isLoading = false;
    }
  }

  function addExportMsg(filename, rows, sessionId) {
    const chat = document.getElementById('chat');
    const empty = document.getElementById('empty-state');
    if (empty) empty.remove();

    const url = `/download-excel?session_id=${encodeURIComponent(sessionId)}&filename=${encodeURIComponent(filename)}`;

    const div = document.createElement('div');
    div.className = 'msg ai';
    div.innerHTML = `
      <div class="bubble">
        ✅ Siap di-download — <strong>${rows.toLocaleString('id-ID')}</strong> baris data.<br>
        <a class="dl-btn" href="${url}" download="${filename}">
          <svg viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
          Download Excel
        </a>
      </div>
      <div class="msg-time">${now()}</div>
    `;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;

    // Auto-trigger download tanpa klik manual
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
  }
</script>
</body>
</html>
"""


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
        log_level="warning",   # kurangi noise di terminal
    )