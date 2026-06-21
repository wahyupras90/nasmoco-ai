import google.generativeai as genai

from config import (
    GEMINI_API_KEY,
    MODEL
)

genai.configure(
    api_key=GEMINI_API_KEY
)


def ask_gemini(prompt: str):

    model = genai.GenerativeModel(
        MODEL
    )

    response = model.generate_content(
        prompt
    )

    return response.text