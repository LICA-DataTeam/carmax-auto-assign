import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.api.services import auto_assign
from src.api.services.auto_assign_store import get_store, reset_store_cache
from src.api.services.auto_reassign import run_auto_reassign


def _write_agents(path: Path, agent_ids: list[str]) -> None:
    payload = {
        "teams": [
            {
                "team": "A",
                "agents": [
                    {
                        "agent_key": agent_id,
                        "agent_name": f"Agent {agent_id}",
                        "target": 30,
                        "min": 30,
                        "max": 30,
                        "active": "true",
                    }
                    for agent_id in agent_ids
                ],
            }
        ]
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.mark.anyio
async def test_auto_reassign_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    state_path = tmp_path / "state.json"
    _write_agents(config_path, ["a1", "b2"])

    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(state_path))
    monkeypatch.setenv("REASSIGN_AFTER_MINUTES", "10")
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("REASSIGN_ELIGIBLE_STATUSES", "C")
    reset_store_cache()

    fixed_now = datetime(2026, 3, 13, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))
    monkeypatch.setattr("src.api.services.auto_reassign._now", lambda: fixed_now)

    store = get_store()
    old_time = fixed_now - timedelta(minutes=11)
    store.record_assignment(
        conv_code="c1",
        agent_id="a1",
        reason="round_robin",
        status="assigned",
        now=old_time,
    )

    class _Ticket:
        def __init__(self, status="C", agent_id=None, department_id=None):
            self.status = status
            self.agent_id = agent_id
            self.department_id = department_id

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(status="C", agent_id="a1")

        async def assign_ticket(self, ticket_id: str, payload):
            return _Ticket(status="C", agent_id=payload.agent_id)

        async def close(self) -> None:
            return None

    monkeypatch.setattr("src.api.services.auto_reassign.LiveAgentClient", _Client)

    result = await run_auto_reassign()
    assert result["checked"] == 1
    assert result["reassigned"] == 1


class _Agent:
    def __init__(self, id, status, online_status):
        self.id = id
        self.status = status
        self.online_status = online_status


@pytest.mark.anyio
async def test_auto_reassign_excludes_offline_employed_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "agents.json"
    state_path = tmp_path / "state.json"
    _write_agents(config_path, ["a1", "b2"])

    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(state_path))
    monkeypatch.setenv("REASSIGN_AFTER_MINUTES", "10")
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("REASSIGN_ELIGIBLE_STATUSES", "C")
    reset_store_cache()

    fixed_now = datetime(2026, 3, 13, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))
    monkeypatch.setattr("src.api.services.auto_reassign._now", lambda: fixed_now)

    store = get_store()
    old_time = fixed_now - timedelta(minutes=11)
    store.record_assignment(
        conv_code="c1",
        agent_id="a1",
        reason="round_robin",
        status="assigned",
        now=old_time,
    )

    class _Ticket:
        def __init__(self, status="C", agent_id=None, department_id=None):
            self.status = status
            self.agent_id = agent_id
            self.department_id = department_id

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(status="C", agent_id="a1")

        async def list_agents(self):
            return [_Agent(id="b2", status="A", online_status="F")]

        async def assign_ticket(self, ticket_id: str, payload):
            return _Ticket(status="C", agent_id=payload.agent_id)

        async def close(self) -> None:
            return None

    monkeypatch.setattr("src.api.services.auto_reassign.LiveAgentClient", _Client)

    captured = {}

    def _plan_next_assignment(*, exclude_agent_ids=None, **kwargs):
        captured["exclude_agent_ids"] = exclude_agent_ids
        return {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "quota_reached",
        }

    monkeypatch.setattr(auto_assign, "plan_next_assignment", _plan_next_assignment)

    result = await run_auto_reassign()
    assert result["checked"] == 1
    # b2 (offline) and a1 (current agent) are both excluded, leaving no candidate.
    assert captured["exclude_agent_ids"] == {"a1", "b2"}


@pytest.mark.anyio
async def test_auto_reassign_threads_department_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    state_path = tmp_path / "state.json"
    _write_agents(config_path, ["a1", "b2"])

    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(state_path))
    monkeypatch.setenv("REASSIGN_AFTER_MINUTES", "10")
    monkeypatch.setenv("REASSIGN_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("REASSIGN_ELIGIBLE_STATUSES", "C")
    reset_store_cache()

    fixed_now = datetime(2026, 3, 13, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))
    monkeypatch.setattr("src.api.services.auto_reassign._now", lambda: fixed_now)

    store = get_store()
    old_time = fixed_now - timedelta(minutes=11)
    store.record_assignment(
        conv_code="c1",
        agent_id="a1",
        reason="round_robin",
        status="assigned",
        now=old_time,
    )

    class _Ticket:
        def __init__(self, status="C", agent_id=None, department_id=None):
            self.status = status
            self.agent_id = agent_id
            self.department_id = department_id

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(status="C", agent_id="a1", department_id="mqemg9w7")

        async def assign_ticket(self, ticket_id: str, payload):
            return _Ticket(status="C", agent_id=payload.agent_id)

        async def close(self) -> None:
            return None

    monkeypatch.setattr("src.api.services.auto_reassign.LiveAgentClient", _Client)

    captured = {}

    def _plan_next_assignment(*, department_id=None, **kwargs):
        captured["department_id"] = department_id
        return {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "unmapped_department",
        }

    monkeypatch.setattr(auto_assign, "plan_next_assignment", _plan_next_assignment)

    result = await run_auto_reassign()
    assert result["checked"] == 1
    assert captured["department_id"] == "mqemg9w7"
