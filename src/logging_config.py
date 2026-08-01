import logging
import logging.handlers
import os

def configure_logging(log_dir="logs", log_file="app.log", level=logging.DEBUG,
                      max_bytes=10 * 1024 * 1024, backup_count=5):
    """
    Configures logging for the application.
    """
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_path = os.path.join(log_dir, log_file)

    # Rotating file handler
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count
    )
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(threadName)s - %(name)s - %(funcName)s - %(message)s'
    )
    handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Root logger
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.addHandler(console_handler)