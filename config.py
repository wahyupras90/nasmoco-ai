from dotenv import load_dotenv
import os

load_dotenv()

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY"
)


# FREE MODEL
SQL_MODEL = (
    "qwen/qwen3-32b"
)

# PREMIUM MODEL
ANALYSIS_MODEL = (
    "anthropic/claude-sonnet-4.5"
)

DB_PATH = "db/nasmoco.db"