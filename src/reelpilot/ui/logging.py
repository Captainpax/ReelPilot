"""Non-blocking rotating file logging configuration."""

from __future__ import annotations

import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Queue


def configure_logging(log_directory: Path) -> tuple[logging.Logger, QueueListener]:
    """Start a queue listener and return the application logger with its owner."""
    log_directory.mkdir(parents=True, exist_ok=True)
    messages: Queue[logging.LogRecord] = Queue()
    file_handler = RotatingFileHandler(
        log_directory / "reelpilot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    listener = QueueListener(messages, file_handler, respect_handler_level=True)
    listener.start()
    logger = logging.getLogger("reelpilot")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(QueueHandler(messages))
    logger.propagate = False
    return logger, listener
