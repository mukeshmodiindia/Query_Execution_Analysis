from __future__ import annotations

import logging
import os
import sys


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(name: str) -> logging.Logger:
    level_name = os.environ.get("QUERY_ANALYSIS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(handler)

    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)

    return logging.getLogger(name)
