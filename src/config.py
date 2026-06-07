import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "autofiera-secret-key")
    DB_PATH = os.getenv("DATABASE_URL", os.path.join(BASE_DIR, "studios.db"))
    DEBUG = os.getenv("FLASK_DEBUG", "1") == "1"
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
    STATIC_FOLDER = os.path.join(BASE_DIR, "static")
