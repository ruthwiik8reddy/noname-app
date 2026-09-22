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

    from .routes.job_record_routes import csrf_token
    app.context_processor(lambda: {"work_records_csrf": csrf_token()})

    app.teardown_appcontext(close_db)

    # Background agent scheduler. Skipped under the reloader's parent process
    # and disabled entirely with AGENT_SCHEDULER=0.
    from .services.agents import start_scheduler
    if start_scheduler(app):
        app.logger.info("Agent scheduler running")

    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=5055)
