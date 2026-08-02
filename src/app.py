from flask import Flask

from .config import Config
from .controller import bp
from .db_manager import close_db
from .logging_config import configure_logging
from .routes import BLUEPRINTS
from .seed import initialize_db


def create_app():
    # Logging first, so schema/migration output during startup is captured.
    configure_logging()

    # Ensure schema exists (including Phase 2/3 tables) and seed data is loaded.
    initialize_db()

    app = Flask(
        __name__,
        template_folder=Config.TEMPLATE_FOLDER,
        static_folder=Config.STATIC_FOLDER,
    )
    app.config.from_object(Config)

    # 16 MB — DVI uploads are multi-photo bursts straight off a phone camera.
    app.config.setdefault("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)

    # Legacy monolith blueprint.
    app.register_blueprint(bp)

    # Phase 1-3 feature blueprints (analytics, dvi, dispatch).
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)

    app.teardown_appcontext(close_db)
    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=5055)
