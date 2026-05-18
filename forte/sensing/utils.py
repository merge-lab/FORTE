"""Small helpers used by the FORTE sensing layer."""

import logging
import time
from datetime import datetime


def setup_logger(log_file: str, logger_name: str) -> logging.Logger:
    """Return a logger writing to ``log_file`` at INFO level."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_file)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger


def get_high_precision_timestamp() -> str:
    """Return a human-readable timestamp with nanosecond precision.

    Format: ``YYYY-MM-DD HH:MM:SS.nnnnnnnnn``.
    """
    ns_since_epoch = time.time_ns()
    seconds, nanoseconds = divmod(ns_since_epoch, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds)
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{nanoseconds:09d}"
