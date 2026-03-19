import pytest

from src.api.services import bq_logger


def test_build_bq_event_filters_none() -> None:
    payload = bq_logger.build_bq_event("evt", conv_code="c1", agent_id=None)
    assert payload["event"] == "evt"
    assert "timestamp" in payload
    assert payload["conv_code"] == "c1"
    assert "agent_id" not in payload


def test_log_bq_event_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Logger:
        enabled = False

        def log(self, payload):
            raise AssertionError("log should not be called when disabled")

    monkeypatch.setattr(bq_logger, "get_bq_logger", lambda: _Logger())
    bq_logger.log_bq_event("evt", conv_code="c1")


def test_bigquery_logger_inserts_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    inserted = {}

    class _Client:
        def __init__(self, project=None, credentials=None):
            self.project = project or "default-project"

        def insert_rows_json(self, table_id, rows):
            inserted["table_id"] = table_id
            inserted["rows"] = rows
            return []

    # Patch google.cloud.bigquery.Client before instantiation
    import google.cloud.bigquery as bigquery

    monkeypatch.setattr(bigquery, "Client", _Client)

    settings = bq_logger.BigQuerySettings(
        enabled=True,
        project_id="test-project",
        dataset="auto_assign",
        table="events",
    )
    logger = bq_logger.BigQueryLogger(settings)
    assert logger.enabled

    payload = bq_logger.build_bq_event("evt", conv_code="c1")
    logger.log(payload)

    assert inserted["table_id"] == "test-project.auto_assign.events"
    assert inserted["rows"][0]["event"] == "evt"
    assert inserted["rows"][0]["conv_code"] == "c1"
