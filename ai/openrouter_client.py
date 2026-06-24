import time
from openai import OpenAI
from config import OPENROUTER_API_KEY

client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    timeout=30,
)

SQL_MODEL      = "qwen/qwen3.7-max"
ANALYSIS_MODEL = "anthropic/claude-sonnet-4-5"


def ask_ai(
    user_prompt,                # str ATAU list[dict] messages array
    system_prompt: str = "",
    mode: str = "sql",
    max_retries: int = 3,
) -> str:
    """
    Kirim prompt ke OpenRouter.
    mode='sql'      → SQL model (Qwen)
    mode='analysis' → Analysis model (Claude)
    Retry otomatis untuk rate limit / timeout.

    user_prompt bisa berupa:
    - str  → dibungkus jadi messages biasa (backward compatible)
    - list → messages array langsung (mendukung cache_control)
    """
    model = SQL_MODEL if mode == "sql" else ANALYSIS_MODEL

    if isinstance(user_prompt, list):
        # Messages array sudah lengkap (termasuk system + cache_control)
        messages = user_prompt
    else:
        # Backward compatible: string prompt biasa
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

    last_error = None
    for attempt in range(max_retries):
        try:
            print(f"MODEL={model}")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1 if mode == "sql" else 0.3,
                max_tokens=4096,
            )
            return response.choices[0].message.content

        except Exception as e:
            last_error = e
            err_str = str(e).lower()

            # Rate limit → tunggu lebih lama
            if "429" in err_str or "rate limit" in err_str:
                wait = 5 * (attempt + 1)
                print(f"  Rate limit. Tunggu {wait}s...")
                time.sleep(wait)

            # Timeout / server error → retry cepat
            elif "timeout" in err_str or "5" in str(getattr(e, 'status_code', '')):
                wait = 2 ** attempt
                print(f"  Timeout/server error. Retry {attempt+1}/{max_retries} ({wait}s)...")
                time.sleep(wait)

            # Error lain → langsung raise
            else:
                raise

    raise RuntimeError(f"Gagal setelah {max_retries} percobaan: {last_error}")