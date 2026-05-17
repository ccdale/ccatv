from __future__ import annotations

import logging


def configure_logging(level_name: str = "INFO") -> None:
    """Initialize process-wide logging once at app startup."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
