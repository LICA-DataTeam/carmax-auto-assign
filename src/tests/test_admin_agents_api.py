from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routes import admin_agents
from src.api.services.agent_config_admin import (
    AgentMutationResult,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)


def _client() -> TestClient:
    return TestClient(app)


def test_admin_get_agents_unauthorized(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    client = _client()
    response = client.get("/admin/agents")
    assert response.status_code == 401


def test_admin_get_agents_success(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)
    payload = {
        "version": 5,
        "updated_at": "2026-05-05T00:00:00Z",
        "updated_by": "admin@company.com",
        "change_reason": "test",
        "teams": [{"team": "A", "agents": [{"agent_key": "a1", "agent_name": "A1", "active": True}]}],
        "agents": [{"team": "A", "agent_key": "a1", "agent_name": "A1", "active": True}],
    }
    monkeypatch.setattr(admin_agents.agent_config_admin, "get_agent_config", lambda: payload)

    client = _client()
    response = client.get("/admin/agents", headers={"Authorization": "Bearer token-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 5
    assert body["agents"][0]["agent_key"] == "a1"


def test_admin_create_agent_success(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    result = AgentMutationResult(
        config={
            "version": 2,
            "updated_at": "2026-05-05T00:00:00Z",
            "updated_by": "token_admin",
            "change_reason": "add",
            "teams": [{"team": "A", "agents": [{"agent_key": "a1", "agent_name": "A1", "active": True}]}],
            "agents": [{"team": "A", "agent_key": "a1", "agent_name": "A1", "active": True}],
        },
        changed_agent={"team": "A", "agent_key": "a1", "agent_name": "A1", "active": True},
    )
    monkeypatch.setattr(admin_agents.agent_config_admin, "create_agent", lambda **kwargs: result)

    client = _client()
    response = client.post(
        "/admin/agents",
        headers={"Authorization": "Bearer token-123"},
        json={
            "team": "A",
            "agent_key": "a1",
            "agent_name": "A1",
            "active": True,
            "expected_version": 1,
            "change_reason": "add",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["config"]["version"] == 2


def test_admin_patch_agent_version_conflict(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    def _raise_conflict(**kwargs):
        raise VersionConflictError("version conflict")

    monkeypatch.setattr(admin_agents.agent_config_admin, "update_agent", _raise_conflict)

    client = _client()
    response = client.patch(
        "/admin/agents/a1",
        headers={"Authorization": "Bearer token-123"},
        json={"expected_version": 1, "change_reason": "rename", "agent_name": "A1-new"},
    )
    assert response.status_code == 409


def test_admin_delete_agent_not_found(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    def _raise_not_found(**kwargs):
        raise NotFoundError("missing")

    monkeypatch.setattr(admin_agents.agent_config_admin, "delete_agent", _raise_not_found)

    client = _client()
    response = client.request(
        "DELETE",
        "/admin/agents/a404",
        headers={"Authorization": "Bearer token-123"},
        json={"expected_version": 3, "change_reason": "remove", "mode": "remove"},
    )
    assert response.status_code == 404


def test_admin_create_agent_validation_error(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    def _raise_validation(**kwargs):
        raise ValidationError("bad payload")

    monkeypatch.setattr(admin_agents.agent_config_admin, "create_agent", _raise_validation)

    client = _client()
    response = client.post(
        "/admin/agents",
        headers={"Authorization": "Bearer token-123"},
        json={
            "team": "A",
            "agent_key": "a1",
            "agent_name": "A1",
            "active": True,
            "expected_version": 1,
            "change_reason": "add",
        },
    )
    assert response.status_code == 400


def test_admin_create_agent_uses_mapped_bearer_actor(monkeypatch) -> None:
    monkeypatch.delenv(admin_agents.ADMIN_TOKENS_ENV, raising=False)
    monkeypatch.setenv(admin_agents.ADMIN_TOKEN_MAP_ENV, '{"token-abc":"owner@company.com"}')
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    captured = {}

    def _create_agent(**kwargs):
        captured["actor"] = kwargs.get("actor")
        return AgentMutationResult(
            config={
                "version": 2,
                "updated_at": "2026-05-05T00:00:00Z",
                "updated_by": str(kwargs.get("actor")),
                "change_reason": "add",
                "teams": [{"team": "A", "agents": [{"agent_key": "a1", "agent_name": "A1", "active": True}]}],
                "agents": [{"team": "A", "agent_key": "a1", "agent_name": "A1", "active": True}],
            },
            changed_agent={"team": "A", "agent_key": "a1", "agent_name": "A1", "active": True},
        )

    monkeypatch.setattr(admin_agents.agent_config_admin, "create_agent", _create_agent)

    client = _client()
    response = client.post(
        "/admin/agents",
        headers={"Authorization": "Bearer token-abc"},
        json={
            "team": "A",
            "agent_key": "a1",
            "agent_name": "A1",
            "active": True,
            "expected_version": 1,
            "change_reason": "add",
        },
    )
    assert response.status_code == 200
    assert captured["actor"] == "owner@company.com"
