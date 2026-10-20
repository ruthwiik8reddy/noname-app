import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for minimal envs
    def load_dotenv() -> bool:
        return False

load_dotenv()


class Config:
    # ------------------------------------------------------------------
    # Core application
    # ------------------------------------------------------------------

    # IMPORTANT:
    # Set a strong random SECRET_KEY in .env for production.
    # Example:
    # SECRET_KEY=<generate-a-long-random-value>
    SECRET_KEY = os.getenv("SECRET_KEY", "")

    # No hardcoded fallback: a key that lives in the repo lets anyone forge a
    # session. Without one in .env we generate a random key per process, so
    # local dev still works — logins just don't survive a restart.
    if not SECRET_KEY:
        import secrets as _secrets
        SECRET_KEY = _secrets.token_hex(32)
        SECRET_KEY_IS_EPHEMERAL = True
    else:
        SECRET_KEY_IS_EPHEMERAL = False

    DB_PATH = os.getenv(
        "DATABASE_URL",
        os.path.join(BASE_DIR, "studios.db")
    )

    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
    STATIC_FOLDER = os.path.join(BASE_DIR, "static")

    # ------------------------------------------------------------------
    # Session security
    # ------------------------------------------------------------------

    # JavaScript cannot read the authentication cookie.
    SESSION_COOKIE_HTTPONLY = True

    # Helps prevent cross-site cookie abuse while allowing normal navigation.
    SESSION_COOKIE_SAMESITE = "Lax"

    # Set SESSION_COOKIE_SECURE=1 in production when using HTTPS.
    SESSION_COOKIE_SECURE = (
        os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    )

    # Session lifetime.
    # Default: 8 hours.
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(
            os.getenv("SESSION_LIFETIME_MINUTES", "480")
        )
    )

    # Refresh the lifetime while the user is active.
    SESSION_REFRESH_EACH_REQUEST = True

    # ------------------------------------------------------------------
    # Authentication security
    # ------------------------------------------------------------------

    # Maximum failed login attempts during the login window.
    AUTH_MAX_LOGIN_ATTEMPTS = int(
        os.getenv("AUTH_MAX_LOGIN_ATTEMPTS", "5")
    )

    # Login-attempt tracking window.
    # Default: 15 minutes.
    AUTH_LOGIN_WINDOW_SECONDS = int(
        os.getenv("AUTH_LOGIN_WINDOW_SECONDS", "900")
    )

    # Temporary lockout after too many failed attempts.
    # Default: 15 minutes.
    AUTH_LOCKOUT_SECONDS = int(
        os.getenv("AUTH_LOCKOUT_SECONDS", "900")
    )

    # ------------------------------------------------------------------
    # AI config
    # ------------------------------------------------------------------

    OLLAMA_URL = os.getenv(
        "OLLAMA_URL",
        "http://127.0.0.1:11434"
    )

    OLLAMA_MODEL = os.getenv(
        "OLLAMA_MODEL",
        "llama3.1:8b"
    )

    # Multimodal model for Digital Vehicle Inspection.
    OLLAMA_VISION_MODEL = os.getenv(
        "OLLAMA_VISION_MODEL",
        "qwen2.5vl:7b"
    )

    # Local inference is slow — vision especially.
    OLLAMA_TEXT_TIMEOUT = int(
        os.getenv("OLLAMA_TEXT_TIMEOUT", "90")
    )

    OLLAMA_VISION_TIMEOUT = int(
        os.getenv("OLLAMA_VISION_TIMEOUT", "180")
    )

    # Backend selection: "ollama" or "llamacpp".
    LLM_BACKEND = os.getenv(
        "LLM_BACKEND",
        "ollama"
    )

    # Smaller model for lightweight jobs.
    OLLAMA_FAST_MODEL = os.getenv(
        "OLLAMA_FAST_MODEL",
        ""
    )

    # ------------------------------------------------------------------
    # llama.cpp
    # ------------------------------------------------------------------

    LLAMACPP_URL = os.getenv(
        "LLAMACPP_URL",
        ""
    )

    LLAMACPP_MODEL = os.getenv(
        "LLAMACPP_MODEL",
        "local"
    )

    LLAMACPP_FAST_URL = os.getenv(
        "LLAMACPP_FAST_URL",
        ""
    )

    LLAMACPP_FAST_MODEL = os.getenv(
        "LLAMACPP_FAST_MODEL",
        ""
    )

    LLAMACPP_VISION_URL = os.getenv(
        "LLAMACPP_VISION_URL",
        ""
    )

    LLAMACPP_VISION_MODEL = os.getenv(
        "LLAMACPP_VISION_MODEL",
        "local-vision"
    )

    # Maximum number of simultaneous inference calls.
    LLM_MAX_CONCURRENT = int(
        os.getenv("LLM_MAX_CONCURRENT", "2")
    )

    # ------------------------------------------------------------------
    # AI global switch
    # ------------------------------------------------------------------

    AI_ENABLED = os.getenv(
        "AI_ENABLED",
        "1"
    )

    # ------------------------------------------------------------------
    # Gemini
    # ------------------------------------------------------------------

    GEMINI_API_KEY = os.getenv(
        "GEMINI_API_KEY",
        ""
    )

    GEMINI_MODEL = os.getenv(
        "GEMINI_MODEL",
        "gemini-2.0-flash"
    )

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    OPENAI_API_KEY = os.getenv(
        "OPENAI_API_KEY",
        ""
    )

    OPENAI_MODEL = os.getenv(
        "OPENAI_MODEL",
        "gpt-4o-mini"
    )

    # ------------------------------------------------------------------
    # SMS / Twilio
    # ------------------------------------------------------------------

    # Never commit real credentials to GitHub.
    TWILIO_ACCOUNT_SID = os.getenv(
        "TWILIO_ACCOUNT_SID",
        ""
    )

    TWILIO_AUTH_TOKEN = os.getenv(
        "TWILIO_AUTH_TOKEN",
        ""
    )

    TWILIO_FROM_NUMBER = os.getenv(
        "TWILIO_FROM_NUMBER",
        ""
    )

    APP_BASE_URL = os.getenv(
        "APP_BASE_URL",
        "http://localhost:5000"
    )

    # ------------------------------------------------------------------
    # Upload limits
    # ------------------------------------------------------------------

    # DVI uploads can contain multiple phone-camera images.
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # Private storage outside /static — invoice scans and other files that
    # must never be reachable by a guessable URL.
    PRIVATE_STORAGE = os.getenv(
        "PRIVATE_STORAGE",
        os.path.join(BASE_DIR, "instance", "private")
    )