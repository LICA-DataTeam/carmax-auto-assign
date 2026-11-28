"""Optional office-hours window for the reassign sweep (active_from / active_to,
Asia/Manila "HH:MM"). Outside it the sweep is skipped like when disabled;
with no window set there is no time restriction."""

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routes import admin_agents
from src.api.services import agent_config_admin, auto_assign, auto_reassign, reassign_settings, reassign_sweep
from src.api.services.auto_assign_store import reset_store_cache
from src.api.services.reassign_settings import ReassignSettings


MANILA = ZoneInfo("Asia/Manila")
WINDOW_ENV = ["REASSIGN_ACTIVE_FROM", "REASSIGN_ACTIVE_TO"]


def _at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, 28, hh, mm, tzinfo=MANILA)


def _settings(**window) -> ReassignSettings:
    return ReassignSettings(
        enabled=True, after_minutes=5, max_attempts=2, eligible_statuses=("N",),
        max_age_minutes=120, max_per_run=50, **window,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in WINDOW_ENV + ["REASSIGN_ENABLED"]:
        monkeypatch.delenv(name, raising=False)
    reassign_settings.reset_cache()
    reassign_sweep.reset_local_state()
    yield
    reassign_settings.reset_cache()
    reassign_sweep.reset_local_state()


# --- window logic -------------------------------------------------------------


def test_no_window_means_no_time_restriction() -> None:
    settings = _settings()
    assert all(settings.is_active_at(_at(h)) for h in range(24))


@pytest.mark.parametrize(
    "hh, mm, expected",
    [(8, 59, False), (9, 0, True), (12, 30, True), (17, 59, True), (18, 0, False), (23, 0, False), (0, 0, False)],
)
def test_daytime_window_start_inclusive_end_exclusive(hh, mm, expected) -> None:
    assert _settings(active_from="09:00", active_to="18:00").is_active_at(_at(hh, mm)) is expected


@pytest.mark.parametrize(
    "hh, expected", [(21, False), (22, True), (23, True), (0, True), (5, True), (6, False), (12, False)]
)
def test_overnight_window_spans_midnight(hh, expected) -> None:
    assert _settings(active_from="22:00", active_to="06:00").is_active_at(_at(hh)) is expected


def test_window_is_evaluated_in_manila_time() -> None:
    settings = _settings(active_from="09:00", active_to="18:00")
    # 02:00 UTC = 10:00 Manila (inside); 12:00 UTC = 20:00 Manila (outside).
    assert settings.is_active_at(datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)) is True
    assert settings.is_active_at(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)) is False


def test_half_set_window_is_ignored_not_blocking() -> None:
    assert _settings(active_from="09:00").is_active_at(_at(3)) is True


@pytest.mark.parametrize("raw, expected", [("9:00", "09:00"), ("09:00", "09:00"), ("23:59", "23:59"),
                                           ("24:00", None), ("9", None), ("09:0", None), ("ab:cd", None), (900, None)])
def test_hhmm_parsing(raw, expected) -> None:
    assert reassign_settings._parse_hhmm(raw) == expected


# --- precedence -----------------------------------------------------------------


def test_window_from_firestore_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("REASSIGN_ACTIVE_FROM", "07:00")
    monkeypatch.setenv("REASSIGN_ACTIVE_TO", "20:00")
    monkeypatch.setattr(reassign_settings, "_load_doc", lambda: {"active_from": "09:00", "active_to": "18:00"})
    settings = reassign_settings.load_settings(use_cache=False)
    assert (settings.active_from, settings.active_to) == ("09:00", "18:00")
    assert settings.sources["active_from"] == "firestore"


def test_window_falls_back_to_env_then_none(monkeypatch) -> None:
    monkeypatch.setattr(reassign_settings, "_load_doc", lambda: None)
    assert reassign_settings.load_settings(use_cache=False).active_from is None
    monkeypatch.setenv("REASSIGN_ACTIVE_FROM", "8:30")
    monkeypatch.setenv("REASSIGN_ACTIVE_TO", "17:30")
    settings = reassign_settings.load_settings(use_cache=False)
    assert (settings.active_from, settings.active_to) == ("08:30", "17:30")
    assert settings.sources["active_to"] == "env"


def test_explicit_null_in_doc_clears_env_window(monkeypatch) -> None:
    monkeypatch.setenv("REASSIGN_ACTIVE_FROM", "09:00")
    monkeypatch.setenv("REASSIGN_ACTIVE_TO", "18:00")
    monkeypatch.setattr(reassign_settings, "_load_doc", lambda: {"active_from": None, "active_to": None})
    settings = reassign_settings.load_settings(use_cache=False)
    assert settings.active_from is None and settings.active_to is None
    assert settings.sources["active_from"] == "firestore"


# --- /admin/reassign ----------------------------------------------------------------


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, db, key):
        self._db, self._key = db, key

    def get(self, transaction=None):
        return _Snap(self._db.docs.get(self._key))


class _Collection:
    def __init__(self, db, name):
        self._db, self._name = db, name

    def document(self, doc_id=None):
        return _Doc(self._db, (self._name, doc_id or f"auto-{len(self._db.docs)}"))


class _Txn:
    def __init__(self, db):
        self._db = db

    def set(self, ref, data, merge=False):
        self._db.docs[ref._key] = data


class _FakeDb:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self, name)

    def transaction(self):
        return _Txn(self)


class _FakeFirestoreModule:
    @staticmethod
    def transactional(fn):
        return fn


KEY = ("app_config", "reassign_settings")
AUTH = {"Authorization": "Bearer token-123"}


@pytest.fixture
def admin(monkeypatch):
    db = _FakeDb()
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "firestore")
    monkeypatch.setattr(agent_config_admin, "_get_firestore_client", lambda: (db, _FakeFirestoreModule))
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)
    return TestClient(app), db


def _patch(client, version, **fields):
    return client.patch(
        "/admin/reassign", headers=AUTH,
        json={"expected_version": version, "change_reason": "office hours", **fields},
    )


def test_admin_sets_shows_and_keeps_window(admin) -> None:
    client, db = admin
    assert _patch(client, 0, enabled=True, active_from="9:00", active_to="18:00").status_code == 200
    assert db.docs[KEY]["active_from"] == "09:00" and db.docs[KEY]["active_to"] == "18:00"

    body = client.get("/admin/reassign", headers=AUTH).json()
    assert body["settings"]["active_from"] == "09:00"
    assert body["settings"]["active_to"] == "18:00"

    # A PATCH that doesn't mention the window keeps it.
    assert _patch(client, 1, after_minutes=7).status_code == 200
    assert db.docs[KEY]["active_from"] == "09:00" and db.docs[KEY]["after_minutes"] == 7


def test_admin_clears_window_with_null(admin) -> None:
    client, db = admin
    _patch(client, 0, active_from="09:00", active_to="18:00")
    response = _patch(client, 1, active_from=None, active_to=None)
    assert response.status_code == 200
    assert db.docs[KEY]["active_from"] is None and db.docs[KEY]["active_to"] is None
    reassign_settings.reset_cache()
    assert reassign_settings.load_settings().is_active_at(_at(3)) is True


@pytest.mark.parametrize(
    "fields",
    [
        {"active_from": "09:00"},  # only one bound
        {"active_to": "18:00"},
        {"active_from": "09:00", "active_to": "09:00"},  # empty window
        {"active_from": "25:00", "active_to": "18:00"},
        {"active_from": "9am", "active_to": "6pm"},
    ],
)
def test_admin_rejects_bad_windows(admin, fields) -> None:
    client, db = admin
    assert _patch(client, 0, **fields).status_code == 400
    assert KEY not in db.docs


def test_admin_rejects_clearing_only_one_bound(admin) -> None:
    client, db = admin
    _patch(client, 0, active_from="09:00", active_to="18:00")
    assert _patch(client, 1, active_to=None).status_code == 400
    assert db.docs[KEY]["active_to"] == "18:00"


# --- sweep is skipped outside the window ------------------------------------------


@pytest.fixture
def sweep(tmp_path, monkeypatch):
    (tmp_path / "agents.json").write_text(
        json.dumps({"teams": [{"team": "A", "agents": [{"agent_key": "a1", "agent_name": "A1", "active": "true"}]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(tmp_path / "agents.json"))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(tmp_path / "state.json"))
    reset_store_cache()
    runs = []

    async def _fake_run(**kwargs):
        runs.append(kwargs)
        return {"status": "ok", "checked": 0, "reassigned": 0, "results": []}

    monkeypatch.setattr(auto_reassign, "run_auto_reassign", _fake_run)
    state_path = tmp_path / "state.json"

    def _configure(settings, now):
        monkeypatch.setattr(reassign_settings, "load_settings", lambda use_cache=True: settings)
        monkeypatch.setattr(reassign_sweep, "_now", lambda: now)

    def _lease_written():
        return state_path.exists() and "reassign_lease" in json.loads(state_path.read_text(encoding="utf-8"))

    return _configure, runs, _lease_written


def test_traffic_sweep_skipped_outside_window_like_disabled(sweep) -> None:
    configure, runs, lease_written = sweep
    configure(_settings(active_from="09:00", active_to="18:00"), _at(20))
    assert reassign_sweep.maybe_run_sweep()["status"] == "outside_active_hours"
    assert runs == []
    assert not lease_written()  # no lease taken, same as disabled


def test_traffic_sweep_runs_inside_window(sweep) -> None:
    configure, runs, lease_written = sweep
    configure(_settings(active_from="09:00", active_to="18:00"), _at(10))
    assert reassign_sweep.maybe_run_sweep()["status"] == "ok"
    assert len(runs) == 1 and lease_written()


def test_traffic_sweep_unrestricted_without_window(sweep) -> None:
    configure, runs, _ = sweep
    configure(_settings(), _at(3))
    assert reassign_sweep.maybe_run_sweep()["status"] == "ok"
    assert len(runs) == 1


def test_auto_reassign_endpoint_skipped_outside_window(sweep, monkeypatch) -> None:
    configure, runs, lease_written = sweep
    configure(_settings(active_from="09:00", active_to="18:00"), _at(7))
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    response = TestClient(app).post("/auto-reassign", headers={"X-CMX-Auto-Assign-Secret": "secret"})
    assert response.status_code == 200
    assert response.json()["status"] == "outside_active_hours"
    assert runs == [] and not lease_written()


def test_disabled_still_wins_over_window(sweep) -> None:
    configure, runs, _ = sweep
    off = ReassignSettings(**{**_settings(active_from="09:00", active_to="18:00").to_dict(),
                              "eligible_statuses": ("N",), "enabled": False})
    configure(off, _at(10))
    assert reassign_sweep.maybe_run_sweep()["status"] == "disabled"
    assert runs == []
