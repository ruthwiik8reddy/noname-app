import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for minimal envs
    def load_dotenv() -> bool:
        return False

load_dotenv()


class Config:
    SECRET_KEY      = os.getenv("SECRET_KEY", "autofiera-secret-key")
    DB_PATH         = os.getenv("DATABASE_URL", os.path.join(BASE_DIR, "studios.db"))
    DEBUG           = os.getenv("FLASK_DEBUG", "1") == "1"
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
    STATIC_FOLDER   = os.path.join(BASE_DIR, "static")

    # AI config
    OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

    # Multimodal model for Digital Vehicle Inspection (Phase 2).
    # Install with: ollama pull llava   (or llava:13b / bakllava for better accuracy)
    OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llava")

    # Local inference is slow — vision especially. Generous, but bounded.
    OLLAMA_TEXT_TIMEOUT   = int(os.getenv("OLLAMA_TEXT_TIMEOUT", "90"))
    OLLAMA_VISION_TIMEOUT = int(os.getenv("OLLAMA_VISION_TIMEOUT", "180"))

    # Global kill switch. Set AI_ENABLED=0 to run the app with every AI feature
    # degrading to its deterministic fallback — useful for demos and CI.
    AI_ENABLED = os.getenv("AI_ENABLED", "1")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # SMS / Twilio config
    # Set these in your .env file — never commit real credentials
    TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_NUMBER  = os.getenv("TWILIO_FROM_NUMBER", "")
    APP_BASE_URL        = os.getenv("APP_BASE_URL", "http://localhost:5000")