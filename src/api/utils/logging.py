from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(fields)
    logger.info(json.dumps(payload, ensure_ascii=True, default=str))
