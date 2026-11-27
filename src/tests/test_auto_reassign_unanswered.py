"""Auto-reassign of unanswered (status N) leads: eligibility, the per-ticket
attempt cap, the max-age window, bot-only records, per-run cap, and the
full_pool fallback when the stale ticket's department has no one else."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

from src.api.services import auto_assign
from src.api.services.auto_assign_store import get_store, reset_store_cache
from src.api.services.auto_reassign import run_auto_reassign


T0 = datetime(2026, 11, 27, 10, 0, tzinfo=ZoneInfo("Asia/Manila"))


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_ids: list[str],
    *,
    after: str = "3",
    attempts: str = "2",
    statuses: str = "N",
    max_age: Optional[str] = None,
    per_run: Optional[str] = None,
    departments: Optional[dict] = None,
) -> None:
    config_path = tmp_path / "agents.json"
    routing_path = tmp_path / "routing.json"
    config_path.write_text(
        json.dumps(
            {
                "teams": [
                    {
                        "team": "A",
                        "agents": [
                            {"agent_key": a, "agent_name": f"Agent {a}", "max": 30, "active": "true"}
                            for a in agent_ids
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
    monkeypatch.setenv("REASSIGN_AFTER_MINUTES", after)
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", attempts)
    monkeypatch.setenv("REASSIGN_ELIGIBLE_STATUSES", statuses)
    for name, value in (("REASSIGN_MAX_AGE_MINUTES", max_age), ("REASSIGN_MAX_PER_RUN", per_run)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("LIVEAGENT_REASSIGN_STATUS", "N")
    reset_store_cache()


def _bot_assign(conv_code: str, agent_id: str, at: datetime) -> None:
    get_store().commit_assignment(
        conv_code=conv_code,
        agent_id=agent_id,
        reason="round_robin",
        date_key=at.strftime("%Y-%m-%d"),
        next_index=None,
        max_assignments=None,
        now=at,
    )


class _Ticket:
    def __init__(self, status: str, agent_id: Optional[str], department_id: Optional[str] = None):
        self.status = status
        self.agent_id = agent_id
        self.department_id = department_id
        self.owner_name = None


class _FakeLiveAgent:
    """Tracks the ticket's current agent; status stays N unless told otherwise."""

    tickets: dict = {}
    get_calls: list = []
    assign_calls: list = []
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1

    async def get_ticket(self, conv_code: str):
        type(self).get_calls.append(conv_code)
        return type(self).tickets[conv_code]

    async def list_agents(self):
        return []

    async def assign_ticket(self, conv_code: str, payload):
        type(self).assign_calls.append((conv_code, payload.agent_id, payload.status))
        ticket = type(self).tickets[conv_code]
        ticket.agent_id = payload.agent_id
        return ticket

    async def close(self) -> None:
        return None


@pytest.fixture
def liveagent(monkeypatch: pytest.MonkeyPatch):
    _FakeLiveAgent.tickets = {}
    _FakeLiveAgent.get_calls = []
    _FakeLiveAgent.assign_calls = []
    _FakeLiveAgent.instances = 0
    monkeypatch.setattr("src.api.services.auto_reassign.LiveAgentClient", _FakeLiveAgent)
    return _FakeLiveAgent


def _at(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr("src.api.services.auto_reassign._now", lambda: when)


@pytest.mark.anyio
async def test_unanswered_status_n_lead_is_reassigned(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"])
    _bot_assign("c1", "a1", T0)
    liveagent.tickets["c1"] = _Ticket("N", "a1")

    _at(monkeypatch, T0 + timedelta(minutes=2))
    early = await run_auto_reassign()
    assert early["checked"] == 0  # not stale yet

    _at(monkeypatch, T0 + timedelta(minutes=3))
    result = await run_auto_reassign()
    assert result["reassigned"] == 1
    assert liveagent.assign_calls == [("c1", "b2", "N")]


@pytest.mark.anyio
async def test_answered_lead_is_not_reassigned_and_is_remembered(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"])
    _bot_assign("c1", "a1", T0)
    liveagent.tickets["c1"] = _Ticket("A", "a1")  # agent replied

    _at(monkeypatch, T0 + timedelta(minutes=5))
    first = await run_auto_reassign()
    assert first["reassigned"] == 0
    assert first["results"][0]["reason"] == "status_not_eligible"
    assert get_store().get_assignment("c1")["ticket_status"] == "A"

    # Next scan filters it out without calling LiveAgent again.
    _at(monkeypatch, T0 + timedelta(minutes=6))
    second = await run_auto_reassign()
    assert second["checked"] == 0
    assert liveagent.get_calls == ["c1"]


@pytest.mark.anyio
async def test_lead_cannot_bounce_more_than_max_attempts(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2", "c3"], attempts="2", max_age="600")
    _bot_assign("c1", "a1", T0)
    liveagent.tickets["c1"] = _Ticket("N", "a1")

    reassigned = 0
    for minute in range(3, 60, 3):  # scheduler keeps firing
        _at(monkeypatch, T0 + timedelta(minutes=minute))
        reassigned += (await run_auto_reassign())["reassigned"]

    assert reassigned == 2
    assert len(liveagent.assign_calls) == 2
    assert get_store().get_assignment("c1")["reassign_count"] == 2


def test_store_rejects_reassign_past_limit(monkeypatch, tmp_path) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"])
    _bot_assign("c1", "a1", T0)
    store = get_store()
    kwargs = dict(previous_agent_id="a1", reason="reassign_timeout", date_key="2026-11-27",
                  next_index=None, max_reassign_count=1, max_assignments=None, now=T0)
    assert store.commit_reassign(conv_code="c1", new_agent_id="b2", **kwargs)["status"] == "reassigned"
    before = store.get_daily_state("2026-11-27")["counts"]
    again = store.commit_reassign(conv_code="c1", new_agent_id="a1", **kwargs)
    assert again["reason"] == "reassign_limit_reached"
    # A rejected reassign must not consume the target agent's daily quota.
    assert store.get_daily_state("2026-11-27")["counts"] == before


@pytest.mark.anyio
async def test_max_attempts_zero_disables_without_liveagent_calls(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"], attempts="0")
    _bot_assign("c1", "a1", T0)
    liveagent.tickets["c1"] = _Ticket("N", "a1")
    _at(monkeypatch, T0 + timedelta(minutes=30))
    result = await run_auto_reassign()
    assert result["checked"] == 0
    assert liveagent.instances == 0


@pytest.mark.anyio
async def test_old_history_outside_max_age_is_ignored(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"], max_age="60")
    _bot_assign("old", "a1", T0 - timedelta(days=30))  # never answered, months-old
    _bot_assign("recent", "a1", T0 - timedelta(minutes=10))
    liveagent.tickets["old"] = _Ticket("N", "a1")
    liveagent.tickets["recent"] = _Ticket("N", "a1")
    _at(monkeypatch, T0)
    result = await run_auto_reassign()
    assert result["checked"] == 1
    assert liveagent.get_calls == ["recent"]


@pytest.mark.anyio
async def test_manually_assigned_tickets_are_never_reassigned(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"])
    get_store().record_assignment(
        conv_code="manual", agent_id="a1", reason="ticket_already_assigned",
        status="already_assigned", now=T0,
    )
    liveagent.tickets["manual"] = _Ticket("N", "a1")
    _at(monkeypatch, T0 + timedelta(minutes=10))
    result = await run_auto_reassign()
    assert result["checked"] == 0


@pytest.mark.anyio
async def test_per_run_cap_processes_oldest_first(monkeypatch, tmp_path, liveagent) -> None:
    _setup(tmp_path, monkeypatch, ["a1", "b2"], per_run="2")
    for i, minutes_ago in enumerate([10, 30, 20]):
        _bot_assign(f"c{i}", "a1", T0 - timedelta(minutes=minutes_ago))
        liveagent.tickets[f"c{i}"] = _Ticket("N", "a1")
    _at(monkeypatch, T0)
    result = await run_auto_reassign()
    assert result["checked"] == 2
    assert liveagent.get_calls == ["c1", "c2"]  # 30 and 20 minutes old


@pytest.mark.anyio
async def test_stale_direct_page_lead_falls_back_to_full_pool(monkeypatch, tmp_path, liveagent) -> None:
    _setup(
        tmp_path, monkeypatch, ["angelo", "o1"],
        departments={"mqemg9w7": {"mode": "direct", "agent_keys": ["angelo"], "active": True}},
    )
    _bot_assign("c1", "angelo", T0)
    liveagent.tickets["c1"] = _Ticket("N", "angelo", department_id="mqemg9w7")
    _at(monkeypatch, T0 + timedelta(minutes=3))
    result = await run_auto_reassign()
    assert result["reassigned"] == 1
    assert liveagent.assign_calls == [("c1", "o1", "N")]
