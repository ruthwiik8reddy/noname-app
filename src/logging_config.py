import logging
import logging.handlers
import os
import re


class ApprovalTokenFilter(logging.Filter):
    def filter(self,record):
        record.msg=re.sub(r"/approvals/[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", "/approvals/[redacted]", record.getMessage())
        record.args=()
        return True


def configure_logging(log_dir="logs", log_file="app.log", level=logging.INFO,
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
    handler.addFilter(ApprovalTokenFilter())

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(ApprovalTokenFilter())

    # Root logger
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.addHandler(console_handler)
    # Third-party libraries are extremely chatty at DEBUG. Watchdog in
    # particular writes to logs/app.log, which lives inside the directory it
    # is watching — so its own log lines trigger more filesystem events.
    for noisy in ("watchdog", "fsevents", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Don't stack duplicate handlers when Flask's reloader re-runs the factory.
    logger.propagate = True