"""
test_cache.py
=============
Verifikasi prompt caching bekerja di OpenRouter untuk qwen/qwen3.7-max.
Jalankan 2x — run pertama = cache write, run kedua = cache hit.

Usage:
    cd D:\AI_nasmoco
    python test_cache.py
"""

import time
from pathlib import Path
from openai import OpenAI
from config import OPENROUTER_API_KEY

client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    timeout=60,
)

SQL_MODEL  = "qwen/qwen3.7-max"
SQL_PROMPT = Path("prompts/sql_prompt.txt").read_text(encoding="utf-8")

messages = [
    {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": SQL_PROMPT,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    },
    {
        "role": "user",
        "content": "Pertanyaan user:\nBerapa total CPUS bulan ini?\n\nTUGAS:\nBuat 1 query SQL saja."
    }
]

print(f"SQL_PROMPT length : {len(SQL_PROMPT)} chars")
print(f"Model             : {SQL_MODEL}")
print("-" * 50)

for run in range(1, 3):
    print(f"\n🔄 RUN {run}...")
    t0 = time.time()

    response = client.chat.completions.create(
        model=SQL_MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=200,
    )

    elapsed = time.time() - t0
    usage   = response.usage

    print(f"⏱  Waktu respons    : {elapsed:.1f} detik")
    print(f"📥 Prompt tokens    : {usage.prompt_tokens}")
    print(f"📤 Completion tokens: {usage.completion_tokens}")

    # Cache info — bisa muncul di prompt_tokens_details atau langsung di usage
    details = getattr(usage, "prompt_tokens_details", None)
    if details:
        cached = getattr(details, "cached_tokens", 0)
        print(f"💾 Cached tokens    : {cached}")
        if cached and cached > 0:
            print(f"✅ CACHE HIT! Hemat {cached / usage.prompt_tokens * 100:.0f}% dari prompt tokens")
        else:
            print(f"📝 Cache write (belum hit — normal untuk run pertama)")
    else:
        # Coba dari raw response jika SDK tidak expose details
        raw = response.model_dump() if hasattr(response, "model_dump") else {}
        usage_raw = raw.get("usage", {})
        details_raw = usage_raw.get("prompt_tokens_details", {})
        cached = details_raw.get("cached_tokens", 0)
        print(f"💾 Cached tokens    : {cached}")
        if cached and cached > 0:
            print(f"✅ CACHE HIT! Hemat {cached / usage.prompt_tokens * 100:.0f}% dari prompt tokens")
        else:
            print(f"📝 Cache write (belum hit — normal untuk run pertama)")

    print(f"\nSQL output:\n{response.choices[0].message.content[:200]}...")

    if run == 1:
        print("\n⏳ Tunggu 2 detik sebelum run kedua...")
        time.sleep(2)

print("\n" + "=" * 50)
print("Selesai. Bandingkan 'Cached tokens' run 1 vs run 2.")
print("Run 2 seharusnya ada cached_tokens > 0 jika cache bekerja.")