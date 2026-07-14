from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routes import admin_agents, admin_departments
from src.api.services import auto_assign
from src.api.services.department_routing_admin import (
    DepartmentMutationResult,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)


def _client() -> TestClient:
    return TestClient(app)


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)


def test_admin_get_departments_unauthorized(monkeypatch) -> None:
    monkeypatch.setenv(admin_agents.ADMIN_TOKENS_ENV, "token-123")
    monkeypatch.delenv(admin_agents.ADMIN_TOKEN_MAP_ENV, raising=False)
    monkeypatch.delenv(admin_agents.ADMIN_ALLOWED_PRINCIPALS_ENV, raising=False)

    client = _client()
    response = client.get("/admin/departments")
    assert response.status_code == 401


def test_admin_get_departments_success(monkeypatch) -> None:
    _auth_env(monkeypatch)
    payload = {
        "version": 3,
        "updated_at": "2026-07-13T00:00:00Z",
        "updated_by": "admin@company.com",
        "change_reason": "seed",
        "departments": {
            "o6woli37": {"label": "CarMax AutoCenter", "mode": "full_pool", "agent_keys": [], "active": True},
        },
        "departments_list": [
            {
                "department_id": "o6woli37",
                "label": "CarMax AutoCenter",
                "mode": "full_pool",
                "agent_keys": [],
                "active": True,
            }
        ],
    }
    monkeypatch.setattr(
        admin_departments.department_routing_admin, "get_department_routing_config", lambda: payload
    )

    client = _client()
    response = client.get("/admin/departments", headers={"Authorization": "Bearer token-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 3
    assert body["departments_list"][0]["department_id"] == "o6woli37"


def test_admin_upsert_department_success(monkeypatch) -> None:
    _auth_env(monkeypatch)

    result = DepartmentMutationResult(
        config={
            "version": 1,
            "updated_at": "2026-07-13T00:00:00Z",
            "updated_by": "token_admin",
            "change_reason": "pilot",
            "departments": {
                "mqemg9w7": {
                    "label": "CMC - Angelo Hernaez",
                    "mode": "direct",
                    "agent_keys": ["i3gpqj30"],
                    "active": True,
                }
            },
            "departments_list": [
                {
                    "department_id": "mqemg9w7",
                    "label": "CMC - Angelo Hernaez",
                    "mode": "direct",
                    "agent_keys": ["i3gpqj30"],
                    "active": True,
                }
            ],
        },
        changed_department={
            "department_id": "mqemg9w7",
            "label": "CMC - Angelo Hernaez",
            "mode": "direct",
            "agent_keys": ["i3gpqj30"],
            "active": True,
        },
    )
    monkeypatch.setattr(
        admin_departments.department_routing_admin, "upsert_department_route", lambda **kwargs: result
    )

    client = _client()
    response = client.post(
        "/admin/departments/mqemg9w7",
        headers={"Authorization": "Bearer token-123"},
        json={
            "label": "CMC - Angelo Hernaez",
            "mode": "direct",
            "agent_keys": ["i3gpqj30"],
            "active": True,
            "expected_version": 0,
            "change_reason": "pilot",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["department"]["department_id"] == "mqemg9w7"


def test_admin_upsert_department_version_conflict(monkeypatch) -> None:
    _auth_env(monkeypatch)

    def _raise_conflict(**kwargs):
        raise VersionConflictError("version conflict")

    monkeypatch.setattr(
        admin_departments.department_routing_admin, "upsert_department_route", _raise_conflict
    )

    client = _client()
    response = client.patch(
        "/admin/departments/mqemg9w7",
        headers={"Authorization": "Bearer token-123"},
        json={
            "label": "CMC - Angelo Hernaez",
            "mode": "direct",
            "agent_keys": ["i3gpqj30"],
            "active": True,
            "expected_version": 0,
            "change_reason": "pilot",
        },
    )
    assert response.status_code == 409


def test_admin_upsert_department_default_rejected(monkeypatch) -> None:
    _auth_env(monkeypatch)

    def _raise_validation(**kwargs):
        raise ValidationError("department_id 'default' is reserved and cannot be routed")

    monkeypatch.setattr(
        admin_departments.department_routing_admin, "upsert_department_route", _raise_validation
    )

    client = _client()
    response = client.post(
        "/admin/departments/default",
        headers={"Authorization": "Bearer token-123"},
        json={
            "label": "Fallback",
            "mode": "full_pool",
            "agent_keys": [],
            "active": True,
            "expected_version": 0,
            "change_reason": "test",
        },
    )
    assert response.status_code == 400


def test_admin_upsert_department_rejects_admin_login_key(monkeypatch) -> None:
    _auth_env(monkeypatch)

    def _raise_validation(**kwargs):
        raise ValidationError("agent_key '1h7uz719' is the shared admin login and cannot be assignable")

    monkeypatch.setattr(
        admin_departments.department_routing_admin, "upsert_department_route", _raise_validation
    )

    client = _client()
    response = client.post(
        "/admin/departments/mqemg9w7",
        headers={"Authorization": "Bearer token-123"},
        json={
            "label": "CMC - Angelo Hernaez",
            "mode": "direct",
            "agent_keys": ["1h7uz719"],
            "active": True,
            "expected_version": 0,
            "change_reason": "test",
        },
    )
    assert response.status_code == 400


def test_admin_preview_department_success(monkeypatch) -> None:
    _auth_env(monkeypatch)

    captured = {}

    def _plan_next_assignment(*, now=None, department_id=None, exclude_agent_ids=None, owner_name=None):
        captured["now"] = now
        captured["department_id"] = department_id
        captured["exclude_agent_ids"] = exclude_agent_ids
        return {
            "status": "candidate",
            "agent_id": "i3gpqj30",
            "reason": "direct_department",
            "day_key": "2026-07-14",
            "next_index": None,
            "pool_key": "direct:mqemg9w7",
            "bypass_quota": True,
        }

    monkeypatch.setattr(auto_assign, "plan_next_assignment", _plan_next_assignment)

    client = _client()
    response = client.get(
        "/admin/departments/mqemg9w7/preview",
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["department_id"] == "mqemg9w7"
    assert body["status"] == "candidate"
    assert body["agent_id"] == "i3gpqj30"
    assert body["bypass_quota"] is True
    assert captured["department_id"] == "mqemg9w7"
    assert captured["now"] is None
    assert captured["exclude_agent_ids"] is None


def test_admin_preview_department_never_mutates(monkeypatch) -> None:
    _auth_env(monkeypatch)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("preview must never call commit/assign logic")

    monkeypatch.setattr(auto_assign, "commit_assignment", _fail_if_called)
    monkeypatch.setattr(
        auto_assign,
        "plan_next_assignment",
        lambda **kwargs: {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "unmapped_department",
        },
    )

    client = _client()
    response = client.get(
        "/admin/departments/unknown/preview",
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "no_eligible_agents"


def test_admin_preview_department_with_exclude_and_simulated_time(monkeypatch) -> None:
    _auth_env(monkeypatch)

    captured = {}

    def _plan_next_assignment(*, now=None, department_id=None, exclude_agent_ids=None, owner_name=None):
        captured["now"] = now
        captured["exclude_agent_ids"] = exclude_agent_ids
        return {"status": "no_eligible_agents", "agent_id": None, "reason": "direct_no_alternative_agent"}

    monkeypatch.setattr(auto_assign, "plan_next_assignment", _plan_next_assignment)

    client = _client()
    response = client.get(
        "/admin/departments/mqemg9w7/preview"
        "?now=2026-07-14T09:00:00%2B08:00&exclude_agent_ids=i3gpqj30,other",
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code == 200
    assert captured["now"] is not None
    assert captured["exclude_agent_ids"] == {"i3gpqj30", "other"}


def test_admin_preview_department_invalid_now_rejected(monkeypatch) -> None:
    _auth_env(monkeypatch)

    client = _client()
    response = client.get(
        "/admin/departments/mqemg9w7/preview?now=not-a-date",
        headers={"Authorization": "Bearer token-123"},
    )
    assert response.status_code == 400


def test_admin_delete_department_not_found(monkeypatch) -> None:
    _auth_env(monkeypatch)

    def _raise_not_found(**kwargs):
        raise NotFoundError("missing")

    monkeypatch.setattr(
        admin_departments.department_routing_admin, "delete_department_route", _raise_not_found
    )

    client = _client()
    response = client.request(
        "DELETE",
        "/admin/departments/unknown",
        headers={"Authorization": "Bearer token-123"},
        json={"expected_version": 1, "change_reason": "cleanup", "mode": "remove"},
    )
    assert response.status_code == 404
