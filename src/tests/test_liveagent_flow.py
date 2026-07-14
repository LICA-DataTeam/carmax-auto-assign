import pytest

from src.api.routes import assign as assign_route
from src.api.services import auto_assign
from src.api.services.auto_assign_store import reset_store_cache


class _Ticket:
    def __init__(self, agent_id=None, department_id=None, owner_name=None):
        self.agent_id = agent_id
        self.department_id = department_id
        self.owner_name = owner_name


@pytest.mark.anyio
async def test_existing_assignment_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    reset_store_cache()

    monkeypatch.setattr(
        auto_assign,
        "get_existing_assignment",
        lambda conv_code: {"agent_id": "a1"},
    )

    class _FailClient:
        def __init__(self) -> None:
            raise AssertionError("LiveAgentClient should not be created")

    monkeypatch.setattr(assign_route, "LiveAgentClient", _FailClient)

    result = await assign_route.assign(
        conv_code="c1",
        agent_id=None,
        x_cmx_auto_assign_secret="secret",
    )
    assert result.status == "already_assigned"
    assert result.agent_id == "a1"
    assert result.reason == "idempotent_replay"


@pytest.mark.anyio
async def test_remote_ticket_already_assigned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    reset_store_cache()
    monkeypatch.setattr(auto_assign, "get_existing_assignment", lambda conv_code: None)

    recorded = {}

    def _record_existing_assignment(*, conv_code: str, agent_id: str, reason: str):
        recorded["conv_code"] = conv_code
        recorded["agent_id"] = agent_id
        recorded["reason"] = reason
        return {"agent_id": agent_id}

    monkeypatch.setattr(auto_assign, "record_existing_assignment", _record_existing_assignment)

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(agent_id="remote1")

        async def assign_ticket(self, ticket_id: str, payload):
            raise AssertionError("assign_ticket should not be called")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(assign_route, "LiveAgentClient", _Client)

    result = await assign_route.assign(
        conv_code="c2",
        agent_id=None,
        x_cmx_auto_assign_secret="secret",
    )
    assert result.status == "already_assigned"
    assert result.agent_id == "remote1"
    assert result.reason == "ticket_already_assigned"
    assert recorded["conv_code"] == "c2"
    assert recorded["agent_id"] == "remote1"


@pytest.mark.anyio
async def test_liveagent_success_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    reset_store_cache()
    monkeypatch.setattr(auto_assign, "get_existing_assignment", lambda conv_code: None)

    plan = {
        "status": "candidate",
        "agent_id": "a1",
        "reason": "round_robin",
        "day_key": "2026-03-13",
        "next_index": 0,
    }
    monkeypatch.setattr(auto_assign, "plan_next_assignment", lambda **kwargs: plan)

    committed = {}

    def _commit_assignment(**kwargs):
        committed.update(kwargs)
        return {
            "status": "assigned",
            "conv_code": kwargs["conv_code"],
            "agent_id": kwargs["agent_id"],
            "reason": kwargs["reason"],
        }

    monkeypatch.setattr(auto_assign, "commit_assignment", _commit_assignment)

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(agent_id=None)

        async def assign_ticket(self, ticket_id: str, payload):
            return _Ticket(agent_id=payload.agent_id)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(assign_route, "LiveAgentClient", _Client)

    result = await assign_route.assign(
        conv_code="c3",
        agent_id=None,
        x_cmx_auto_assign_secret="secret",
    )
    assert result.status == "assigned"
    assert result.agent_id == "a1"
    assert committed["conv_code"] == "c3"
    assert committed["agent_id"] == "a1"
    assert committed["day_key"] == "2026-03-13"


@pytest.mark.anyio
async def test_liveagent_failure_does_not_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    reset_store_cache()
    monkeypatch.setattr(auto_assign, "get_existing_assignment", lambda conv_code: None)
    monkeypatch.setattr(
        auto_assign,
        "plan_next_assignment",
        lambda **kwargs: {
            "status": "candidate",
            "agent_id": "a1",
            "reason": "round_robin",
            "day_key": "2026-03-13",
            "next_index": 0,
        },
    )

    def _commit_assignment(**kwargs):
        raise AssertionError("commit_assignment should not be called on failure")

    monkeypatch.setattr(auto_assign, "commit_assignment", _commit_assignment)

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(agent_id=None)

        async def assign_ticket(self, ticket_id: str, payload):
            raise RuntimeError("LiveAgent failure")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(assign_route, "LiveAgentClient", _Client)

    result = await assign_route.assign(
        conv_code="c4",
        agent_id=None,
        x_cmx_auto_assign_secret="secret",
    )
    assert result.status == "failed"
    assert result.reason == "liveagent_error"


@pytest.mark.anyio
async def test_department_id_threads_into_plan_next_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    reset_store_cache()
    monkeypatch.setattr(auto_assign, "get_existing_assignment", lambda conv_code: None)

    captured = {}

    def _plan_next_assignment(*, department_id=None, **kwargs):
        captured["department_id"] = department_id
        return {
            "status": "candidate",
            "agent_id": "angelo",
            "reason": "direct_department",
            "day_key": "2026-03-13",
            "next_index": None,
            "pool_key": "direct:mqemg9w7",
            "bypass_quota": True,
        }

    monkeypatch.setattr(auto_assign, "plan_next_assignment", _plan_next_assignment)

    committed = {}

    def _commit_assignment(**kwargs):
        committed.update(kwargs)
        return {
            "status": "assigned",
            "conv_code": kwargs["conv_code"],
            "agent_id": kwargs["agent_id"],
            "reason": kwargs["reason"],
        }

    monkeypatch.setattr(auto_assign, "commit_assignment", _commit_assignment)

    class _Client:
        async def get_ticket(self, ticket_id: str):
            return _Ticket(agent_id=None, department_id="mqemg9w7")

        async def assign_ticket(self, ticket_id: str, payload):
            return _Ticket(agent_id=payload.agent_id)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(assign_route, "LiveAgentClient", _Client)

    result = await assign_route.assign(
        conv_code="c5",
        agent_id=None,
        x_cmx_auto_assign_secret="secret",
    )
    assert result.status == "assigned"
    assert result.agent_id == "angelo"
    assert captured["department_id"] == "mqemg9w7"
    assert committed["pool_key"] == "direct:mqemg9w7"
    assert committed["bypass_quota"] is True
