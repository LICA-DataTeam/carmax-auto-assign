from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _disable_bigquery_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # .env may set BQ_ENABLED=true for the real app; tests must never write to
    # production BigQuery regardless of what a developer's local .env says.
    monkeypatch.setenv("BQ_ENABLED", "false")
