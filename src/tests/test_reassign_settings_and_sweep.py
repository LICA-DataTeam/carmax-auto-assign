"""Reassign settings (Firestore over env), /admin/reassign, the traffic-triggered
sweep with its cross-instance 60s lease, and the rules for which leads move."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routes import admin_agents
from src.api.routes import assign as assign_route
from src.api.services import agent_config_admin, auto_assign, auto_reassign, reassign_settings, reassign_sweep
from src.api.services.auto_assign_store import get_store, lease_decision, reset_store_cache
from src.api.services.reassign_settings import ReassignSettings


T0 = datetime(2026, 11, 28, 10, 0, tzinfo=ZoneInfo("Asia/Manila"))
ENV_VARS = [
    "REASSIGN_ENABLED",
    "REASSIGN_AFTER_MINUTES",
    "REASSIGN_MAX_ATTEMPTS",
    "REASSIGN_ELIGIBLE_STATUSES",
    "REASSIGN_MAX_AGE_MINUTES",
    "REASSIGN_MAX_PER_RUN",
]
ON = ReassignSettings(
    enabled=True, after_minutes=5, max_attempts=2, eligible_statuses=("N",),
    max_age_minutes=120, max_per_run=50,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    reassign_settings.reset_cache()
    reassign_sweep.reset_local_state()
    yield
    reassign_settings.reset_cache()
    reassign_sweep.reset_local_state()


def _setup_store(tmp_path: Path, monkeypatch, agents: list[dict], departments: Optional[dict] = None) -> None:
    config_path = tmp_path / "agents.json"
    routing_path = tmp_path / "routing.json"
    config_path.write_text(
        json.dumps(
            {
                "teams": [
                    {
                        "team": "A",
                        "agents": [
                            {"agent_name": f"Agent {a['agent_key']}", "active": "true", "max": 30, **a}
                            for a in agents
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    routing_path.write_text(json.dumps({"departments": departments or {}}), encoding="utf-8")
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv(auto_assign.DEPARTMENT_ROUTING_CONFIG_ENV, str(routing_path))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("LIVEAGENT_REASSIGN_STATUS", "N")
    reset_store_cache()


# ============================================================================
# Settings: Firestore over env, missing doc -> env (off)
# ============================================================================


def test_missing_doc_falls_back_to_env_and_is_off(monkeypatch) -> None:
    monkeypatch.setattr(reassign_settings, "_load_doc", lambda: None)
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", "0")  # what Cloud Run sets today
    monkeypatch.setenv("REASSIGN_ELIGIBLE_STATUSES", "C,A")
    settings = reassign_settings.load_settings(use_cache=False)
    assert settings.enabled is False
    assert settings.max_attempts == 0
    assert settings.eligible_statuses == ("C", "A")
    assert settings.sources["enabled"] == "default"
    assert settings.sources["max_attempts"] == "env"


def test_firestore_values_override_env(monkeypatch) -> None:
    monkeypatch.setenv("REASSIGN_AFTER_MINUTES", "10")
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("REASSIGN_MAX_PER_RUN", "7")
    monkeypatch.setattr(
        reassign_settings,
        "_load_doc",
        lambda: {"enabled": True, "after_minutes": 5, "max_attempts": 2, "eligible_statuses": ["n"], "max_age_minutes": 120},
    )
    settings = reassign_settings.load_settings(use_cache=False)
    assert settings.enabled is True
    assert settings.after_minutes == 5
    assert settings.max_attempts == 2
    assert settings.eligible_statuses == ("N",)
    assert settings.max_per_run == 7  # not in the doc -> env
    assert settings.sources["after_minutes"] == "firestore"
    assert settings.sources["max_per_run"] == "env"


def test_invalid_firestore_value_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("REASSIGN_AFTER_MINUTES", "9")
    monkeypatch.setattr(reassign_settings, "_load_doc", lambda: {"after_minutes": "soon", "enabled": "maybe"})
    settings = reassign_settings.load_settings(use_cache=False)
    assert settings.after_minutes == 9
    assert settings.enabled is False


def test_firestore_read_error_fails_closed(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(reassign_settings, "_load_doc", _boom)
    assert reassign_settings.load_settings(use_cache=False).enabled is False


def test_file_config_source_never_reads_firestore(monkeypatch) -> None:
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setattr(
        agent_config_admin, "_get_firestore_client", lambda: pytest.fail("must not touch Firestore")
    )
    assert reassign_settings._load_doc() is None


def test_settings_are_cached_briefly(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(reassign_settings, "_load_doc", lambda: calls.append(1) or {"enabled": True})
    reassign_settings.load_settings()
    reassign_settings.load_settings()
    assert len(calls) == 1


# ============================================================================
# /admin/reassign (versioned, Firestore-backed) - fake Firestore
# ============================================================================


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


SETTINGS_KEY = ("app_config", "reassign_settings")


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDb()
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "firestore")
    monkeypatch.setattr(agent_config_admin, "_get_firestore_client", lambda: (db, _FakeFirestoreModule))
    return db


def _admin(monkeypatch) -> TestClient:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)
    return TestClient(app)


AUTH = {"Authorization": "Bearer token-123"}


def test_admin_get_without_doc_shows_env_fallback(monkeypatch, fake_db) -> None:
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", "0")
    response = _admin(monkeypatch).get("/admin/reassign", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False
    assert body["version"] == 0
    assert body["settings"]["enabled"] is False
    assert body["settings"]["max_attempts"] == 0
    assert body["sources"]["max_attempts"] == "env"


def test_admin_patch_creates_then_partially_updates(monkeypatch, fake_db) -> None:
    client = _admin(monkeypatch)
    first = client.patch(
        "/admin/reassign",
        headers=AUTH,
        json={
            "expected_version": 0, "change_reason": "turn on reassign",
            "enabled": True, "after_minutes": 5, "max_attempts": 2,
            "eligible_statuses": ["N"], "max_age_minutes": 120, "max_per_run": 50,
        },
    )
    assert first.status_code == 200, first.text
    doc = fake_db.docs[SETTINGS_KEY]
    assert doc["version"] == 1
    assert doc["enabled"] is True and doc["eligible_statuses"] == ["N"]
    assert doc["updated_by"] == "token_admin"
    audits = [v for (c, _), v in fake_db.docs.items() if c == "reassign_settings_audit"]
    assert len(audits) == 1 and audits[0]["changes"]["after_minutes"] == 5

    second = client.patch(
        "/admin/reassign", headers=AUTH,
        json={"expected_version": 1, "change_reason": "slower", "after_minutes": 7},
    )
    assert second.status_code == 200
    doc = fake_db.docs[SETTINGS_KEY]
    assert doc["version"] == 2
    assert doc["after_minutes"] == 7
    assert doc["max_attempts"] == 2 and doc["enabled"] is True  # untouched fields kept

    reassign_settings.reset_cache()
    assert reassign_settings.load_settings().after_minutes == 7


def test_admin_patch_version_conflict(monkeypatch, fake_db) -> None:
    fake_db.docs[SETTINGS_KEY] = {"version": 3, "enabled": False}
    response = _admin(monkeypatch).patch(
        "/admin/reassign", headers=AUTH,
        json={"expected_version": 2, "change_reason": "x", "enabled": True},
    )
    assert response.status_code == 409
    assert fake_db.docs[SETTINGS_KEY]["enabled"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"after_minutes": 30, "max_age_minutes": 10},  # window shorter than delay
        {"after_minutes": 0},
        {"max_per_run": 1000},
        {"eligible_statuses": []},
        {},  # nothing to change
    ],
)
def test_admin_patch_rejects_bad_values(monkeypatch, fake_db, body) -> None:
    response = _admin(monkeypatch).patch(
        "/admin/reassign", headers=AUTH, json={"expected_version": 0, "change_reason": "x", **body}
    )
    assert response.status_code in (400, 422)
    assert SETTINGS_KEY not in fake_db.docs


def test_admin_reassign_requires_auth(monkeypatch, fake_db) -> None:
    assert _admin(monkeypatch).get("/admin/reassign").status_code == 401


# ============================================================================
# Lease / throttle
# ============================================================================


def test_lease_rule_interval_and_ttl() -> None:
    kw = dict(holder="b", min_interval_seconds=60, ttl_seconds=300)
    ok, state, _ = lease_decision(None, now=T0, **kw)
    assert ok
    ok2, _, next_allowed = lease_decision(state, now=T0 + timedelta(seconds=59), **kw)
    assert not ok2 and next_allowed == T0 + timedelta(seconds=60)
    # Interval passed but the first sweep still holds an unexpired lease.
    ok3, _, recheck = lease_decision(state, now=T0 + timedelta(seconds=90), **kw)
    assert not ok3 and recheck == T0 + timedelta(seconds=150)  # re-ask after one interval
    # A stuck sweep's lease expires.
    assert lease_decision(state, now=T0 + timedelta(seconds=301), **kw)[0]
    # Released lease: only the interval applies.
    released = {**state, "holder": None, "lease_until": None}
    assert lease_decision(released, now=T0 + timedelta(seconds=61), **kw)[0]


class _Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}])
    monkeypatch.setattr(reassign_settings, "load_settings", lambda use_cache=True: ON)
    clock = _Clock(T0)
    monkeypatch.setattr(reassign_sweep, "_now", clock)
    runs = []

    async def _fake_run(**kwargs):
        runs.append(kwargs)
        return {"status": "ok", "checked": 0, "reassigned": 0, "results": []}

    monkeypatch.setattr(auto_reassign, "run_auto_reassign", _fake_run)
    return clock, runs


def test_no_second_sweep_within_60s_across_instances(sweep_env) -> None:
    clock, runs = sweep_env
    assert reassign_sweep.maybe_run_sweep()["status"] == "ok"

    # Same instance, 10s later: local gate, Firestore not even consulted.
    clock.now = T0 + timedelta(seconds=10)
    assert reassign_sweep.maybe_run_sweep()["status"] == "throttled_local"

    # A different instance (no local memory), 30s later: the shared lease refuses.
    reassign_sweep.reset_local_state()
    clock.now = T0 + timedelta(seconds=30)
    assert reassign_sweep.maybe_run_sweep()["status"] == "throttled"
    assert len(runs) == 1

    # After the interval any instance may sweep again.
    reassign_sweep.reset_local_state()
    clock.now = T0 + timedelta(seconds=61)
    assert reassign_sweep.maybe_run_sweep()["status"] == "ok"
    assert len(runs) == 2


def test_lease_is_released_after_sweep_even_on_error(sweep_env, monkeypatch) -> None:
    clock, _ = sweep_env

    async def _fails(**kwargs):
        raise RuntimeError("liveagent down")

    monkeypatch.setattr(auto_reassign, "run_auto_reassign", _fails)
    assert reassign_sweep.maybe_run_sweep()["status"] == "error"
    lease = json.loads(Path(get_store_path()).read_text(encoding="utf-8"))["reassign_lease"]
    assert lease["holder"] is None


def get_store_path() -> str:
    import os

    return os.environ["AUTO_ASSIGN_STATE_PATH"]


def test_disabled_never_sweeps_or_takes_lease(tmp_path, monkeypatch) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}])
    monkeypatch.setattr(reassign_settings, "load_settings", lambda use_cache=True: ReassignSettings())
    monkeypatch.setattr(auto_reassign, "run_auto_reassign", lambda **kw: pytest.fail("must not run"))
    assert reassign_sweep.maybe_run_sweep()["status"] == "disabled"
    assert "reassign_lease" not in (json.loads(Path(get_store_path()).read_text()) if Path(get_store_path()).exists() else {})


def test_auto_reassign_endpoint_still_works_and_shares_the_lease(sweep_env, monkeypatch) -> None:
    clock, runs = sweep_env
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    client = TestClient(app)
    first = client.post("/auto-reassign", headers={"X-CMX-Auto-Assign-Secret": "secret"})
    assert first.status_code == 200 and first.json()["status"] == "ok"
    # A traffic sweep right after is refused by the same lease.
    reassign_sweep.reset_local_state()
    clock.now = T0 + timedelta(seconds=5)
    assert reassign_sweep.maybe_run_sweep()["status"] == "throttled"
    assert len(runs) == 1
    assert client.post("/auto-reassign").status_code == 401


def test_auto_reassign_endpoint_reports_disabled(tmp_path, monkeypatch) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}])
    monkeypatch.setattr(reassign_settings, "load_settings", lambda use_cache=True: ReassignSettings())
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    response = TestClient(app).post("/auto-reassign", headers={"X-CMX-Auto-Assign-Secret": "secret"})
    assert response.json()["status"] == "disabled"


# ============================================================================
# /auto-assign schedules the sweep without delaying the response
# ============================================================================


class _Ticket:
    def __init__(self, status="N", agent_id=None, department_id=None):
        self.status = status
        self.agent_id = agent_id
        self.department_id = department_id
        self.owner_name = None


class _AssignClient:
    async def get_ticket(self, ticket_id):
        return _Ticket(agent_id="already-there")  # short path: already assigned

    async def close(self):
        return None


@pytest.mark.anyio
async def test_assign_response_is_not_delayed_by_sweep(monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(auto_assign, "get_existing_assignment", lambda conv_code: None)
    monkeypatch.setattr(auto_assign, "record_existing_assignment", lambda **kw: None)
    monkeypatch.setattr(assign_route, "LiveAgentClient", _AssignClient)
    swept = []

    def _slow_sweep():
        time.sleep(2)
        swept.append(1)

    monkeypatch.setattr(reassign_sweep, "maybe_run_sweep", _slow_sweep)

    tasks = BackgroundTasks()
    started = time.perf_counter()
    response = await assign_route.assign(
        conv_code="c1", agent_id=None, x_cmx_auto_assign_secret="secret", background_tasks=tasks
    )
    elapsed = time.perf_counter() - started

    assert response.status == "already_assigned"
    assert elapsed < 0.5  # the 2s sweep did not run inline
    assert swept == []  # not started before the response
    assert [task.func for task in tasks.tasks] == [_slow_sweep]


def test_assign_webhook_runs_sweep_after_response(monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(auto_assign, "get_existing_assignment", lambda conv_code: None)
    monkeypatch.setattr(auto_assign, "record_existing_assignment", lambda **kw: None)
    monkeypatch.setattr(assign_route, "LiveAgentClient", _AssignClient)
    swept = []
    monkeypatch.setattr(reassign_sweep, "maybe_run_sweep", lambda: swept.append(1))

    response = TestClient(app).post(
        "/auto-assign",
        data={"conv_code": "c1"},
        headers={"X-CMX-Auto-Assign-Secret": "secret"},
    )
    assert response.status_code == 200
    assert swept == [1]  # FastAPI injected BackgroundTasks and ran it


# ============================================================================
# Which leads move, and to whom
# ============================================================================


class _FakeLiveAgent:
    tickets: dict = {}
    assigned: list = []
    offline: list = []

    async def get_ticket(self, conv_code):
        return type(self).tickets[conv_code]

    async def list_agents(self):
        return [
            type("A", (), {"id": agent_id, "status": "A", "online_status": "F"})()
            for agent_id in type(self).offline
        ]

    async def assign_ticket(self, conv_code, payload):
        type(self).assigned.append((conv_code, payload.agent_id))
        type(self).tickets[conv_code].agent_id = payload.agent_id
        return type(self).tickets[conv_code]

    async def close(self):
        return None


@pytest.fixture
def liveagent(monkeypatch):
    _FakeLiveAgent.tickets, _FakeLiveAgent.assigned, _FakeLiveAgent.offline = {}, [], []
    monkeypatch.setattr(auto_reassign, "LiveAgentClient", _FakeLiveAgent)
    monkeypatch.setattr(auto_reassign, "_now", lambda: T0)
    return _FakeLiveAgent


@pytest.fixture
def bq_events(monkeypatch):
    events = []
    monkeypatch.setattr(auto_reassign, "log_bq_event", lambda event, **f: events.append((event, f)))
    return events


def _bot_assign(conv_code, agent_id, minutes_ago=6):
    get_store().commit_assignment(
        conv_code=conv_code, agent_id=agent_id, reason="round_robin",
        date_key=T0.strftime("%Y-%m-%d"), next_index=None, max_assignments=None,
        now=T0 - timedelta(minutes=minutes_ago),
    )


def _sweep():
    return asyncio.run(auto_reassign.run_auto_reassign(settings=ON))


def test_unanswered_lead_moves_and_logs_reassigned(tmp_path, monkeypatch, liveagent, bq_events) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}])
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    result = _sweep()
    assert result["reassigned"] == 1
    assert liveagent.assigned == [("c1", "b2")]
    success = [f for e, f in bq_events if e == "auto_reassign_success"]
    assert success == [
        {
            "request_id": success[0]["request_id"], "conv_code": "c1", "agent_id": "b2",
            "reason": "reassigned", "status": "reassigned", "pool_key": "full_pool",
            "previous_agent_id": "a1", "department_id": None, "overflow_from": None,
        }
    ]
    assert get_store().get_assignment("c1")["reason"] == "reassigned"


def test_not_stale_yet_is_left_alone(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}])
    _bot_assign("c1", "a1", minutes_ago=4)  # after_minutes = 5
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    assert _sweep()["checked"] == 0


def test_answered_lead_is_skipped(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}])
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("A", "a1")  # agent replied
    result = _sweep()
    assert result["reassigned"] == 0
    assert result["results"][0]["reason"] == "status_not_eligible"
    assert liveagent.assigned == []


def test_manually_reassigned_lead_is_skipped_for_good(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}, {"agent_key": "c3"}])
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "c3")  # a supervisor moved it to c3
    result = _sweep()
    assert result["results"][0]["reason"] == "agent_changed"
    assert liveagent.assigned == []
    assert get_store().get_assignment("c1")["status"] == "released_from_reassign"
    assert _sweep()["checked"] == 0  # never considered again


def test_unassigned_in_liveagent_is_skipped(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}])
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", None)
    assert _sweep()["results"][0]["reason"] == "agent_changed"
    assert liveagent.assigned == []


def test_current_agent_is_excluded(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}])  # nobody else
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    result = _sweep()
    assert result["reassigned"] == 0
    assert result["results"][0]["status"] == "no_eligible_agents"
    assert liveagent.assigned == []  # never "reassigned" back to a1


def test_offline_agents_are_excluded(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}, {"agent_key": "c3"}])
    liveagent.offline = ["b2"]
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    _sweep()
    assert liveagent.assigned == [("c1", "c3")]


def test_caps_apply_to_reassigned_leads(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(
        tmp_path, monkeypatch,
        [{"agent_key": "a1"}, {"agent_key": "capped", "max": 0}, {"agent_key": "p1"}],
    )
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    _sweep()
    assert liveagent.assigned == [("c1", "p1")]


def test_overflow_applies_to_reassigned_leads(tmp_path, monkeypatch, liveagent, bq_events) -> None:
    _setup_store(
        tmp_path, monkeypatch,
        [
            {"agent_key": "a1"},
            {"agent_key": "capped", "max": 0},
            {"agent_key": "p1"},
            {"agent_key": "ov1", "overflow": True},
        ],
    )
    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    _sweep()
    # Rotation: a1 excluded (current), `capped` is next but at cap -> its turn overflows.
    assert liveagent.assigned == [("c1", "ov1")]
    decision = [f for e, f in bq_events if e == "auto_reassign_decision" and f.get("status") == "candidate"]
    assert decision[0]["reason"] == "overflow_round_robin"
    success = [f for e, f in bq_events if e == "auto_reassign_success"][0]
    assert success["reason"] == "reassigned"
    assert success["overflow_from"] == "capped"
    assert success["previous_agent_id"] == "a1"


def test_department_routing_applies_to_reassigned_leads(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(
        tmp_path, monkeypatch,
        [{"agent_key": "shaira"}, {"agent_key": "win"}, {"agent_key": "other"}],
        departments={"2vhsyd6w": {"mode": "team_pool", "agent_keys": ["shaira", "win"], "active": True}},
    )
    _bot_assign("c1", "shaira")
    liveagent.tickets["c1"] = _Ticket("N", "shaira", department_id="2vhsyd6w")
    _sweep()
    assert liveagent.assigned == [("c1", "win")]  # stays in the team


def test_max_attempts_and_max_age_respected(tmp_path, monkeypatch, liveagent) -> None:
    _setup_store(tmp_path, monkeypatch, [{"agent_key": "a1"}, {"agent_key": "b2"}])
    _bot_assign("old", "a1", minutes_ago=121)  # outside max_age_minutes=120
    liveagent.tickets["old"] = _Ticket("N", "a1")
    assert _sweep()["checked"] == 0

    _bot_assign("c1", "a1")
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    one_attempt = ReassignSettings(**{**ON.to_dict(), "eligible_statuses": ("N",), "max_attempts": 1})
    asyncio.run(auto_reassign.run_auto_reassign(settings=one_attempt))
    monkeypatch.setattr(auto_reassign, "_now", lambda: T0 + timedelta(minutes=10))
    again = asyncio.run(auto_reassign.run_auto_reassign(settings=one_attempt))
    assert again["checked"] == 0
    assert len(liveagent.assigned) == 1
