import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()
class Config:
    SECRET_KEY      = os.getenv("SECRET_KEY", "autofiera-secret-key")
    DB_PATH         = os.getenv("DATABASE_URL", os.path.join(BASE_DIR, "studios.db"))
    DEBUG           = os.getenv("FLASK_DEBUG", "1") == "1"
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
    STATIC_FOLDER   = os.path.join(BASE_DIR, "static")

    # AI config
    OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

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