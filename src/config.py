import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for minimal envs
    def load_dotenv() -> bool:
        return False

load_dotenv()


class Config:
    SECRET_KEY      = os.getenv("SECRET_KEY") or secrets.token_hex(32)
    DB_PATH         = os.getenv("DATABASE_URL", os.path.join(BASE_DIR, "studios.db"))
    DEBUG           = os.getenv("FLASK_DEBUG", "0") == "1"
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

    # Backend selection: "ollama" (default) or "llamacpp".
    LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama")

    # A smaller model for small jobs (drafting a two-line message doesn't need
    # 8B). Falls back to OLLAMA_MODEL when unset.
    OLLAMA_FAST_MODEL = os.getenv("OLLAMA_FAST_MODEL", "")

    # llama.cpp servers — one process per model, so one URL per tier.
    LLAMACPP_URL          = os.getenv("LLAMACPP_URL", "")
    LLAMACPP_MODEL        = os.getenv("LLAMACPP_MODEL", "local")
    LLAMACPP_FAST_URL     = os.getenv("LLAMACPP_FAST_URL", "")
    LLAMACPP_FAST_MODEL   = os.getenv("LLAMACPP_FAST_MODEL", "")
    LLAMACPP_VISION_URL   = os.getenv("LLAMACPP_VISION_URL", "")
    LLAMACPP_VISION_MODEL = os.getenv("LLAMACPP_VISION_MODEL", "local-vision")

    # How many inference calls may be in flight. Match this to the backend's
    # real capacity (OLLAMA_NUM_PARALLEL, or llama-server --parallel).
    LLM_MAX_CONCURRENT = int(os.getenv("LLM_MAX_CONCURRENT", "2"))

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

    PRODUCTION = os.getenv('APP_ENV','development') == 'production'
    SEED_DEMO = os.getenv('SEED_DEMO','0') == '1'
    PRIVATE_UPLOAD_ROOT = os.getenv('PRIVATE_UPLOAD_ROOT',os.path.join(os.path.dirname(DB_PATH),'private_uploads'))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = PRODUCTION
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    @classmethod
    def validate(cls):
        if cls.PRODUCTION:
            from pathlib import Path
            from urllib.parse import urlsplit
            key=os.getenv('SECRET_KEY','')
            if len(key)<32 or key in ('autofiera-secret-key',) or cls.DEBUG or cls.SEED_DEMO:
                raise RuntimeError('Production requires a strong configured SECRET_KEY, debug off and demo seeding off.')
            if urlsplit(cls.APP_BASE_URL).scheme!='https':
                raise RuntimeError('Production APP_BASE_URL must use HTTPS.')
            if Path(cls.PRIVATE_UPLOAD_ROOT).resolve().is_relative_to(Path(cls.STATIC_FOLDER).resolve()):
                raise RuntimeError('Private uploads must be outside the static directory.')
            legacy=Path(cls.STATIC_FOLDER)/'uploads'
            if legacy.exists() and any(p.is_file() and p.name!='.gitkeep' for p in legacy.rglob('*')):
                raise RuntimeError('Move legacy customer uploads out of static storage before production startup.')
