import logging
import logging handlers
import os

def configure_logging(log_dir = "logs" , log_file = "app.log" , level = logging.DEBUG, max_bytes = 10 * 1024 * 1024 , backup_count = 5):
    """
    Configures logging for the application.

    Args:
        log_dir (str): Directory where log files will be stored.
        log_file (str): Name of the log file.
        level (int): Logging level (e.g., logging.INFO, logging.DEBUG).
        max_bytes (int): Maximum size of the log file in bytes before rotation.
        backup_count (int): Number of backup log files to keep.
    """
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_path = os.path.join(log_dir, log_file)

    # Create a rotating file handler
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count
    )
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(threadName)s - %(name)s - %(funcName)s - %(message)s'
    )
    handler.setFormatter(formatter)

    #console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Get the root logger and set its level and handler
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.addHandler(console_handler)

    #logging.getLogger("twilio").setLevel(logging.Error)
    #TODO add in future