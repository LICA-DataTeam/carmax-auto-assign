from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.api.utils.logging import get_logger


logger = get_logger(__name__)


BQ_ENABLED_ENV = "BQ_ENABLED"
BQ_PROJECT_ENV = "BQ_PROJECT_ID"
BQ_DATASET_ENV = "BQ_DATASET"
BQ_TABLE_ENV = "BQ_TABLE"
FIRESTORE_CREDENTIALS_ENV = "CREDENTIALS"


def _parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_credentials() -> tuple[Optional[object], Optional[str]]:
    creds_raw = os.getenv(FIRESTORE_CREDENTIALS_ENV, "").strip()
    if not creds_raw:
        return None, None
    info: Optional[Dict[str, Any]] = None
    if creds_raw.startswith("{"):
        info = json.loads(creds_raw)
    else:
        path = Path(creds_raw)
        if path.exists():
            info = json.loads(path.read_text(encoding="utf-8"))
    if not info:
        return None, None
    try:
        from google.oauth2 import service_account
    except ImportError:
        return None, info.get("project_id")
    credentials = service_account.Credentials.from_service_account_info(info)
    return credentials, info.get("project_id")


@dataclass(frozen=True)
class BigQuerySettings:
    enabled: bool
    project_id: Optional[str]
    dataset: Optional[str]
    table: Optional[str]


def load_bq_settings() -> BigQuerySettings:
    return BigQuerySettings(
        enabled=_parse_bool(os.getenv(BQ_ENABLED_ENV)),
        project_id=os.getenv(BQ_PROJECT_ENV, "").strip() or None,
        dataset=os.getenv(BQ_DATASET_ENV, "").strip() or None,
        table=os.getenv(BQ_TABLE_ENV, "").strip() or None,
    )


class BigQueryLogger:
    def __init__(self, settings: BigQuerySettings) -> None:
        self.enabled = settings.enabled
        self._client = None
        self._table_id = None

        if not settings.enabled:
            return

        if not settings.dataset or not settings.table:
            logger.warning("BigQuery logging disabled: dataset or table not set")
            self.enabled = False
            return

        try:
            from google.cloud import bigquery
        except ImportError as exc:
            logger.warning("BigQuery logging disabled: missing dependency (%s)", exc)
            self.enabled = False
            return

        credentials, project_from_creds = _load_credentials()
        project_id = settings.project_id or project_from_creds
        self._client = bigquery.Client(project=project_id, credentials=credentials)
        project = self._client.project
        self._table_id = f"{project}.{settings.dataset}.{settings.table}"

    def log(self, payload: Dict[str, Any]) -> None:
        if not self.enabled or not self._client or not self._table_id:
            return
        errors = self._client.insert_rows_json(self._table_id, [payload])
        if errors:
            logger.warning("BigQuery insert failed: %s", errors)


_BQ_LOGGER: Optional[BigQueryLogger] = None


def get_bq_logger() -> BigQueryLogger:
    global _BQ_LOGGER
    if _BQ_LOGGER is None:
        _BQ_LOGGER = BigQueryLogger(load_bq_settings())
    return _BQ_LOGGER


def build_bq_event(event: str, **fields: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


def log_bq_event(event: str, **fields: Any) -> None:
    logger_instance = get_bq_logger()
    if not logger_instance.enabled:
        return
    payload = build_bq_event(event, **fields)
    try:
        logger_instance.log(payload)
    except Exception as exc:
        logger.warning("BigQuery logging failed: %s", exc)
