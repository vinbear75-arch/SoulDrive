import logging
import os


def get_logger(name: str) -> logging.Logger:
    configure_default_logging()
    return logging.getLogger(name)


def configure_default_logging():
    root = logging.getLogger()
    if root.handlers:
        return
    level_name = os.environ.get("SOULDRIVE_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
