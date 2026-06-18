import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

class Config:
    SECRET_KEY      = os.getenv("SECRET_KEY", "autofiera-secret-key")
    DB_PATH         = os.getenv("DATABASE_URL", os.path.join(BASE_DIR, "studios.db"))
    DEBUG           = os.getenv("FLASK_DEBUG", "1") == "1"
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
    STATIC_FOLDER   = os.path.join(BASE_DIR, "static")

    # ── AI config ──────────────────────────────────────────────────────────
    # PRIMARY — Ollama (local, free, no internet needed)
    OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://127.0.0.1:11434")
    #OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")   # or mistral, phi3 etc.
    OLLAMA_MODEL = "llama3.2:3b"
    # FALLBACK — Gemini (internet, complex/visual questions)
    #GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_API_KEY = "AIzaSyCxrDnmMcwE90vG9QHdaqeGnahy36wH1eI"
    GEMINI_MODEL   = os.getenv("GEMINI_MODEL",   "gemini-1.5-flash")

    # OPTIONAL — OpenAI (disabled by default)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL   = os.getenv("OPENAI_MODEL",   "gpt-4o-mini")
