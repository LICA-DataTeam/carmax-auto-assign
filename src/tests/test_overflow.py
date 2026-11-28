"""Overflow to group B: when the agent whose full_pool turn it is is at their
daily cap, that turn goes to the next overflow agent (overflow=True) on its own
cursor, and everyone else's turns are unchanged."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routes import admin_agents
from src.api.services import agent_config_admin, auto_assign
from src.api.services.auto_assign_store import (
    FULL_POOL_KEY,
    OVERFLOW_POOL_KEY,
    _daily_updates,
    get_store,
    reset_store_cache,
)


NOW = datetime(2026, 11, 27, 10, 0, tzinfo=ZoneInfo("Asia/Manila"))
DAY = "2026-11-27"


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agents: list[dict],
    departments: Optional[dict] = None,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    reset_store_cache()


def _run(n: int, department_id: Optional[str] = None, prefix: str = "c") -> list[dict]:
    return [
        auto_assign.assign_round_robin(
            conv_code=f"{prefix}{i}", incoming_agent_id=None, now=NOW, department_id=department_id
        )
        for i in range(n)
    ]


def _plan_and_commit(conv_code: str, **plan_kwargs) -> dict:
    plan = auto_assign.plan_next_assignment(now=NOW, **plan_kwargs)
    assert plan["status"] == "candidate", plan
    commit = auto_assign.commit_assignment(
        conv_code=conv_code,
        agent_id=plan["agent_id"],
        reason=plan["reason"],
        now=NOW,
        day_key=plan["day_key"],
        next_index=plan["next_index"],
        pool_key=plan["pool_key"],
        bypass_quota=plan["bypass_quota"],
        max_assignments=plan["max_assignments"],
        extra_cursors=plan.get("extra_cursors"),
    )
    assert commit["status"] == "assigned", commit
    return {**plan, **commit}


# --- config parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected", [(True, True), ("true", True), ("yes", True), (False, False), ("false", False), (None, False)]
)
def test_overflow_flag_parsed_like_active(monkeypatch, tmp_path, raw, expected) -> None:
    agent = {"agent_key": "o1"}
    if raw is not None:
        agent["overflow"] = raw
    _setup(tmp_path, monkeypatch, [agent])
    assert auto_assign.load_agents()[0].overflow is expected


# --- core behavior -----------------------------------------------------------


def test_capped_agents_turn_goes_to_overflow_and_producers_keep_their_share(
    monkeypatch, tmp_path
) -> None:
    roster = ["p1", "cap", "p2", "ov1"]

    # Baseline: nobody capped - plain rotation p1, cap, p2, ov1, ...
    _setup(tmp_path / "base", monkeypatch, [{"agent_key": k} for k in roster])
    baseline = [r["agent_id"] for r in _run(12)]
    assert baseline == roster * 3

    # Rule on: `cap` stops at 2, ov1 is group B.
    _setup(
        tmp_path / "rule",
        monkeypatch,
        [{"agent_key": "p1"}, {"agent_key": "cap", "max": 2}, {"agent_key": "p2"}, {"agent_key": "ov1", "overflow": True}],
    )
    results = _run(12)
    picks = [r["agent_id"] for r in results]

    # `cap` takes its turns at leads 1 and 5; its third turn (lead 9) is past its
    # cap and goes to ov1. Every other slot is identical to the baseline.
    assert picks == ["ov1" if b == "cap" and i >= 8 else b for i, b in enumerate(baseline)]
    base_counts, rule_counts = Counter(baseline), Counter(picks)
    assert rule_counts["p1"] == base_counts["p1"] == 3
    assert rule_counts["p2"] == base_counts["p2"] == 3
    assert rule_counts["cap"] == 2
    assert rule_counts["ov1"] == base_counts["ov1"] + 1

    overflow = [r for r in results if r["reason"] == "overflow_round_robin"]
    assert [r["agent_id"] for r in overflow] == ["ov1"]


def test_overflow_plan_reports_source_and_moves_both_cursors(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "cap", "max": 0}, {"agent_key": "p1"}, {"agent_key": "ov1", "overflow": True}],
    )
    result = _plan_and_commit("c1")
    assert result["reason"] == "overflow_round_robin"
    assert result["agent_id"] == "ov1"
    assert result["overflow_from"] == "cap"
    store = get_store()
    assert store.get_daily_state(DAY, pool_key=FULL_POOL_KEY)["last_index"] == 0  # past `cap`
    assert store.get_daily_state(DAY, pool_key=OVERFLOW_POOL_KEY)["last_index"] == 0  # ov1
    # The next lead is simply the next agent in the main rotation.
    assert _plan_and_commit("c2")["agent_id"] == "p1"


def test_overflow_rotates_evenly_across_group_b(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "c1", "max": 0}, {"agent_key": "c2", "max": 0}]
        + [{"agent_key": k, "overflow": True} for k in ["ov1", "ov2", "ov3"]],
    )
    results = _run(25)
    overflow_picks = [r["agent_id"] for r in results if r["reason"] == "overflow_round_robin"]
    assert len(overflow_picks) == 10  # c1 + c2 turns over 5 rounds
    assert overflow_picks[:6] == ["ov1", "ov2", "ov3", "ov1", "ov2", "ov3"]
    spread = Counter(overflow_picks)
    assert max(spread.values()) - min(spread.values()) <= 1


def test_overflow_cursor_is_independent_of_overflow_agents_main_turns(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "cap", "max": 0}, {"agent_key": "ov1", "overflow": True}, {"agent_key": "ov2", "overflow": True}],
    )
    picks = [(r["agent_id"], r["reason"]) for r in _run(6)]
    assert picks == [
        ("ov1", "overflow_round_robin"),  # cap's turn -> overflow cursor -> ov1
        ("ov1", "round_robin"),  # ov1's own main turn
        ("ov2", "round_robin"),
        ("ov2", "overflow_round_robin"),  # cap's turn -> overflow cursor moves on to ov2
        ("ov1", "round_robin"),
        ("ov2", "round_robin"),
    ]


def test_all_overflow_offline_falls_back_to_next_eligible(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "cap", "max": 0}, {"agent_key": "p1"}, {"agent_key": "ov1", "overflow": True}],
    )
    plan = auto_assign.plan_next_assignment(now=NOW, exclude_agent_ids={"ov1"})
    assert plan["status"] == "candidate"
    assert plan["agent_id"] == "p1"
    assert plan["reason"] == "round_robin"
    assert "overflow_from" not in plan


def test_all_overflow_capped_falls_back_to_next_eligible(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [
            {"agent_key": "cap", "max": 0},
            {"agent_key": "p1"},
            {"agent_key": "ov1", "overflow": True, "max": 0},
            {"agent_key": "ov2", "overflow": True, "max": 0},
        ],
    )
    picks = [(r["agent_id"], r["reason"]) for r in _run(3)]
    assert picks == [("p1", "round_robin")] * 3


def test_everyone_capped_still_reports_quota_reached(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "cap", "max": 0}, {"agent_key": "ov1", "overflow": True, "max": 0}],
    )
    plan = auto_assign.plan_next_assignment(now=NOW)
    assert plan["status"] == "no_eligible_agents"
    assert plan["reason"] == "quota_reached"


def test_offline_agent_without_cap_hit_does_not_trigger_overflow(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "p1"}, {"agent_key": "off"}, {"agent_key": "p2"}, {"agent_key": "ov1", "overflow": True}],
    )
    picks = []
    for i in range(8):
        result = _plan_and_commit(f"c{i}", exclude_agent_ids={"off"})
        assert result["reason"] == "round_robin"
        assert "overflow_from" not in result
        picks.append(result["agent_id"])
    assert picks == ["p1", "p2", "ov1"] * 2 + ["p1", "p2"]


def test_offline_capped_agent_does_not_trigger_overflow(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "cap", "max": 0}, {"agent_key": "p1"}, {"agent_key": "ov1", "overflow": True}],
    )
    plan = auto_assign.plan_next_assignment(now=NOW, exclude_agent_ids={"cap"})
    assert plan["reason"] == "round_robin"
    assert plan["agent_id"] == "p1"


def test_team_fallback_full_pool_also_overflows(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [
            {"agent_key": "shaira"},
            {"agent_key": "cap", "max": 0},
            {"agent_key": "ov1", "overflow": True},
        ],
        departments={"2vhsyd6w": {"mode": "team_pool", "agent_keys": ["shaira"], "active": True}},
    )
    plan = auto_assign.plan_next_assignment(now=NOW, department_id="2vhsyd6w", exclude_agent_ids={"shaira"})
    assert plan["status"] == "candidate"
    assert plan["reason"] == "overflow_round_robin"
    assert plan["agent_id"] == "ov1"
    assert plan["overflow_from"] == "cap"
    assert plan["fallback_from"] == "2vhsyd6w"


# --- direct / owner_override unaffected -------------------------------------


def test_direct_and_owner_override_leads_ignore_overflow(monkeypatch, tmp_path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": "cap", "max": 1}, {"agent_key": "p1"}, {"agent_key": "ov1", "overflow": True}],
        departments={
            "cap-page": {"mode": "direct", "agent_keys": ["cap"], "active": True},
            "shared": {
                "mode": "team_pool",
                "agent_keys": ["cap", "p1"],
                "active": True,
                "owner_overrides": {"cap page": "cap"},
            },
        },
    )
    direct = _run(3, department_id="cap-page", prefix="d")
    assert all(r["agent_id"] == "cap" and r["reason"] == "direct_department" for r in direct)

    override = _plan_and_commit("o1", department_id="shared", owner_name="Cap Page")
    assert override["agent_id"] == "cap"
    assert override["reason"] == "team_pool_owner_override"
    assert "overflow_from" not in override

    # They still count toward the daily total (cap is now over its max)...
    assert get_store().get_daily_state(DAY)["counts"]["cap"] == 4
    # ...and the overflow cursor was never touched by them.
    assert get_store().get_daily_state(DAY, pool_key=OVERFLOW_POOL_KEY)["last_index"] == -1


# --- no overflow agents -> unchanged ------------------------------------------


def test_without_overflow_agents_behavior_is_unchanged(monkeypatch, tmp_path) -> None:
    for name, extra in (("absent", {}), ("false", {"overflow": False})):
        _setup(
            tmp_path / name,
            monkeypatch,
            [{"agent_key": "p1", **extra}, {"agent_key": "cap", "max": 1, **extra}, {"agent_key": "p2", **extra}],
        )
        results = _run(6)
        assert [r["agent_id"] for r in results] == ["p1", "cap", "p2", "p1", "p2", "p1"]
        assert all(r["reason"] == "round_robin" for r in results)
        assert get_store().get_daily_state(DAY, pool_key=OVERFLOW_POOL_KEY)["last_index"] == -1


# --- Firestore write shape ----------------------------------------------------


class _FakeFirestore:
    class Increment:
        def __init__(self, value: int) -> None:
            self.value = value


def test_daily_updates_write_overflow_cursor_as_nested_map() -> None:
    updates = _daily_updates(
        _FakeFirestore, date_key=DAY, agent_id="ov1", next_index=4, pool_key=FULL_POOL_KEY,
        now=NOW, extra_cursors={OVERFLOW_POOL_KEY: 2},
    )
    assert updates["last_index"] == 4
    assert updates["pools"] == {OVERFLOW_POOL_KEY: {"last_index": 2, "last_agent_id": "ov1"}}
    assert not any("." in key for key in updates)


# --- admin API: overflow field --------------------------------------------------


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

    def set(self, ref, data):
        self._db.docs[ref._key] = data


class _FakeDb:
    def __init__(self, roster):
        self.docs = {("app_config", "carmax_agents"): roster}

    def collection(self, name):
        return _Collection(self, name)

    def transaction(self):
        return _Txn(self)


class _FakeFirestoreModule:
    @staticmethod
    def transactional(fn):
        return fn


def _fake_roster(monkeypatch, agents):
    db = _FakeDb({"version": 41, "teams": [{"team": "B", "agents": agents}]})
    monkeypatch.setattr(agent_config_admin, "_get_firestore_client", lambda: (db, _FakeFirestoreModule))
    return db


def _roster_agent(db, key):
    roster = db.docs[("app_config", "carmax_agents")]
    return next(a for t in roster["teams"] for a in t["agents"] if a["agent_key"] == key)


def test_update_agent_keeps_overflow_when_omitted(monkeypatch) -> None:
    db = _fake_roster(
        monkeypatch,
        [{"agent_key": "ov1", "agent_name": "Group B One", "active": True, "max": 300, "overflow": True}],
    )
    result = agent_config_admin.update_agent(
        target_agent_key="ov1", expected_version=41, actor="admin", change_reason="raise max",
        target=300, min_value=0, max_value=350,
    )
    assert _roster_agent(db, "ov1")["overflow"] is True
    assert _roster_agent(db, "ov1")["max"] == 350
    assert result.changed_agent["overflow"] is True

    agent_config_admin.update_agent(
        target_agent_key="ov1", expected_version=42, actor="admin", change_reason="leave group B",
        target=300, min_value=0, max_value=350, overflow=False,
    )
    assert _roster_agent(db, "ov1")["overflow"] is False


def test_create_agent_overflow_defaults_false_and_accepts_true(monkeypatch) -> None:
    db = _fake_roster(monkeypatch, [])
    agent_config_admin.create_agent(
        team="B", agent_key="n1", agent_name="New One", active=True, expected_version=41,
        actor="admin", change_reason="add", target=30, min_value=0, max_value=30,
    )
    agent_config_admin.create_agent(
        team="B", agent_key="n2", agent_name="New Two", active=True, expected_version=42,
        actor="admin", change_reason="add", target=30, min_value=0, max_value=30, overflow=True,
    )
    assert _roster_agent(db, "n1")["overflow"] is False
    assert _roster_agent(db, "n2")["overflow"] is True


def test_get_agent_config_lists_overflow(monkeypatch) -> None:
    _fake_roster(
        monkeypatch,
        [
            {"agent_key": "ov1", "agent_name": "B One", "active": True, "overflow": "true"},
            {"agent_key": "p1", "agent_name": "Producer", "active": True},
        ],
    )
    agents = {a["agent_key"]: a for a in agent_config_admin.get_agent_config()["agents"]}
    assert agents["ov1"]["overflow"] is True
    assert agents["p1"]["overflow"] is False


def _admin_client(monkeypatch) -> TestClient:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)
    return TestClient(app)


def _mutation_result():
    return agent_config_admin.AgentMutationResult(
        config={"version": 42, "teams": [], "agents": []}, changed_agent=None
    )


@pytest.mark.parametrize("body_overflow, expected", [("omit", None), (True, True), (False, False)])
def test_patch_route_passes_overflow_through(monkeypatch, body_overflow, expected) -> None:
    captured = {}

    def _update(**kwargs):
        captured.update(kwargs)
        return _mutation_result()

    monkeypatch.setattr(admin_agents.agent_config_admin, "update_agent", _update)
    body = {"expected_version": 41, "change_reason": "x", "target": 30, "min": 0, "max": 30}
    if body_overflow != "omit":
        body["overflow"] = body_overflow
    response = _admin_client(monkeypatch).patch(
        "/admin/agents/ov1", headers={"Authorization": "Bearer token-123"}, json=body
    )
    assert response.status_code == 200
    assert captured["overflow"] is expected


def test_post_route_passes_overflow_through(monkeypatch) -> None:
    captured = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return _mutation_result()

    monkeypatch.setattr(admin_agents.agent_config_admin, "create_agent", _create)
    response = _admin_client(monkeypatch).post(
        "/admin/agents",
        headers={"Authorization": "Bearer token-123"},
        json={
            "team": "B", "agent_key": "n1", "agent_name": "N1", "active": True, "target": 30,
            "min": 0, "max": 30, "expected_version": 41, "change_reason": "add", "overflow": True,
        },
    )
    assert response.status_code == 200
    assert captured["overflow"] is True
