from flask import Flask
from .config import Config
from .controller import bp
from .db_manager import close_db
from .seed import initialize_db
from src.logging_config import configure_logging


def create_app():
    template_folder = Config.TEMPLATE_FOLDER
    static_folder = Config.STATIC_FOLDER

    # Ensure schema exists and seed data is loaded before the app starts
    initialize_db()

    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
    app.config.from_object(Config)
    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)
    return app


if __name__ == "__main__":
    create_app().run(debug=True, port=5055)
    configure_logging()
