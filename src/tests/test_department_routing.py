import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.api.services import auto_assign
from src.api.services.auto_assign_store import reset_store_cache


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


def _write_routing(path: Path, departments: dict) -> None:
    path.write_text(json.dumps({"departments": departments}, indent=2), encoding="utf-8")


def _setup_env(
    tmp_path: Path,
    agent_ids: list[str],
    departments: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "agents.json"
    routing_path = tmp_path / "routing.json"
    state_path = tmp_path / "state.json"
    _write_agents(config_path, agent_ids)
    _write_routing(routing_path, departments)
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv(auto_assign.DEPARTMENT_ROUTING_CONFIG_ENV, str(routing_path))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(state_path))
    reset_store_cache()


def _now_in_window() -> datetime:
    return datetime(2026, 3, 13, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))


def _now_outside_window() -> datetime:
    return datetime(2026, 3, 13, 8, 0, tzinfo=ZoneInfo("Asia/Manila"))


# --- direct mode -----------------------------------------------------------


def test_direct_mode_bypasses_quota(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["angelo"],
        {"mqemg9w7": {"label": "Angelo", "mode": "direct", "agent_keys": ["angelo"], "active": True}},
        monkeypatch,
    )
    monkeypatch.setattr(auto_assign, "MAX_ASSIGNMENTS_PER_AGENT", 1)
    now = _now_in_window()

    for idx in range(5):
        result = auto_assign.assign_round_robin(
            conv_code=f"c{idx}",
            incoming_agent_id=None,
            now=now,
            department_id="mqemg9w7",
        )
        assert result["status"] == "assigned"
        assert result["agent_id"] == "angelo"


def test_direct_mode_respects_office_hours(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["angelo"],
        {"mqemg9w7": {"label": "Angelo", "mode": "direct", "agent_keys": ["angelo"], "active": True}},
        monkeypatch,
    )
    result = auto_assign.assign_round_robin(
        conv_code="c1",
        incoming_agent_id=None,
        now=_now_outside_window(),
        department_id="mqemg9w7",
    )
    assert result["status"] == "outside_hours"


def test_direct_mode_fails_safe_when_agent_inactive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        [],  # no active agents at all
        {"mqemg9w7": {"label": "Angelo", "mode": "direct", "agent_keys": ["angelo"], "active": True}},
        monkeypatch,
    )
    result = auto_assign.plan_next_assignment(now=_now_in_window(), department_id="mqemg9w7")
    assert result["status"] == "no_eligible_agents"
    assert result["reason"] == "direct_no_alternative_agent"


def test_direct_mode_does_not_fall_back_to_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["b2"],  # active agent exists but is not the department's target
        {"mqemg9w7": {"label": "Angelo", "mode": "direct", "agent_keys": ["angelo"], "active": True}},
        monkeypatch,
    )
    result = auto_assign.plan_next_assignment(now=_now_in_window(), department_id="mqemg9w7")
    assert result["status"] == "no_eligible_agents"
    assert result["reason"] == "direct_no_alternative_agent"
    assert result["agent_id"] is None


# --- team_pool mode ----------------------------------------------------------


def test_team_pool_only_selects_within_department(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2", "c3"],
        {
            "p1ivd5m8": {
                "label": "Team 1",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
            }
        },
        monkeypatch,
    )
    now = _now_in_window()
    picks = [
        auto_assign.assign_round_robin(
            conv_code=f"c{idx}", incoming_agent_id=None, now=now, department_id="p1ivd5m8"
        )["agent_id"]
        for idx in range(4)
    ]
    assert picks == ["a1", "b2", "a1", "b2"]
    assert "c3" not in picks


def test_team_pool_respects_quota_and_does_not_spill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2", "c3"],
        {
            "p1ivd5m8": {
                "label": "Team 1",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
            }
        },
        monkeypatch,
    )
    monkeypatch.setattr(auto_assign, "MAX_ASSIGNMENTS_PER_AGENT", 1)
    now = _now_in_window()

    first = auto_assign.assign_round_robin(
        conv_code="c1", incoming_agent_id=None, now=now, department_id="p1ivd5m8"
    )
    second = auto_assign.assign_round_robin(
        conv_code="c2", incoming_agent_id=None, now=now, department_id="p1ivd5m8"
    )
    assert {first["agent_id"], second["agent_id"]} == {"a1", "b2"}

    third = auto_assign.assign_round_robin(
        conv_code="c3", incoming_agent_id=None, now=now, department_id="p1ivd5m8"
    )
    assert third["status"] == "no_eligible_agents"
    assert third["reason"] == "quota_reached"
    assert third["agent_id"] is None


def test_team_pool_and_full_pool_have_independent_cursors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2", "c3"],
        {
            "p1ivd5m8": {
                "label": "Team 1",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2", "c3"],
                "active": True,
            }
        },
        monkeypatch,
    )
    now = _now_in_window()

    # Interleave team_pool and full_pool calls; each pool should rotate independently
    # starting from its own last_index, not a shared cursor.
    team_1 = auto_assign.assign_round_robin(
        conv_code="t1", incoming_agent_id=None, now=now, department_id="p1ivd5m8"
    )["agent_id"]
    full_1 = auto_assign.assign_round_robin(conv_code="f1", incoming_agent_id=None, now=now)["agent_id"]
    team_2 = auto_assign.assign_round_robin(
        conv_code="t2", incoming_agent_id=None, now=now, department_id="p1ivd5m8"
    )["agent_id"]
    full_2 = auto_assign.assign_round_robin(conv_code="f2", incoming_agent_id=None, now=now)["agent_id"]

    assert [team_1, team_2] == ["a1", "b2"]
    assert [full_1, full_2] == ["a1", "b2"]


# --- team_pool owner_overrides (agent's own FB page inside a shared department) --


def test_team_pool_owner_override_routes_directly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2", "c3"],
        {
            "p1ivd5m8": {
                "label": "CMC - FB Page",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2", "c3"],
                "active": True,
                "owner_overrides": {"Carmax Authorized Agent - B Two": "b2"},
            }
        },
        monkeypatch,
    )
    now = _now_in_window()
    result = auto_assign.plan_next_assignment(
        now=now, department_id="p1ivd5m8", owner_name="Carmax Authorized Agent - B Two"
    )
    assert result["status"] == "candidate"
    assert result["agent_id"] == "b2"
    assert result["reason"] == "team_pool_owner_override"
    assert result["bypass_quota"] is True
    assert result["next_index"] is None


def test_team_pool_owner_override_is_case_and_whitespace_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2"],
        {
            "p1ivd5m8": {
                "label": "CMC - FB Page",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
                "owner_overrides": {"Carmax Authorized Agent - B Two": "b2"},
            }
        },
        monkeypatch,
    )
    result = auto_assign.plan_next_assignment(
        now=_now_in_window(),
        department_id="p1ivd5m8",
        owner_name="  CARMAX AUTHORIZED AGENT - B TWO  ",
    )
    assert result["status"] == "candidate"
    assert result["agent_id"] == "b2"


def test_team_pool_owner_override_bypasses_quota(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2"],
        {
            "p1ivd5m8": {
                "label": "CMC - FB Page",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
                "owner_overrides": {"page-b2": "b2"},
            }
        },
        monkeypatch,
    )
    monkeypatch.setattr(auto_assign, "MAX_ASSIGNMENTS_PER_AGENT", 1)
    now = _now_in_window()
    plan_results = [
        auto_assign.plan_next_assignment(now=now, department_id="p1ivd5m8", owner_name="page-b2")
        for _ in range(5)
    ]
    assert all(r["status"] == "candidate" and r["agent_id"] == "b2" for r in plan_results)


def test_team_pool_owner_override_no_match_falls_to_round_robin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2"],
        {
            "p1ivd5m8": {
                "label": "CMC - FB Page",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
                "owner_overrides": {"page-a1": "a1"},
            }
        },
        monkeypatch,
    )
    now = _now_in_window()
    result = auto_assign.plan_next_assignment(
        now=now, department_id="p1ivd5m8", owner_name="some random customer name"
    )
    assert result["status"] == "candidate"
    assert result["reason"] == "team_pool"
    assert result["agent_id"] == "a1"  # normal round robin, first in rotation


def test_team_pool_owner_override_no_owner_name_falls_to_round_robin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2"],
        {
            "p1ivd5m8": {
                "label": "CMC - FB Page",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
                "owner_overrides": {"page-a1": "a1"},
            }
        },
        monkeypatch,
    )
    result = auto_assign.plan_next_assignment(now=_now_in_window(), department_id="p1ivd5m8")
    assert result["status"] == "candidate"
    assert result["reason"] == "team_pool"


def test_team_pool_owner_override_falls_back_when_target_excluded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2"],
        {
            "p1ivd5m8": {
                "label": "CMC - FB Page",
                "mode": "team_pool",
                "agent_keys": ["a1", "b2"],
                "active": True,
                "owner_overrides": {"page-b2": "b2"},
            }
        },
        monkeypatch,
    )
    result = auto_assign.plan_next_assignment(
        now=_now_in_window(),
        department_id="p1ivd5m8",
        owner_name="page-b2",
        exclude_agent_ids={"b2"},
    )
    # b2 is the override target but excluded (e.g. reassignment) - the team has
    # another member, so this must fall back to round-robin, not fail safe.
    assert result["status"] == "candidate"
    assert result["reason"] == "team_pool"
    assert result["agent_id"] == "a1"


def test_parse_department_routing_owner_override_target_must_be_in_agent_keys() -> None:
    with pytest.raises(ValueError, match="must also be listed in agent_keys"):
        auto_assign._parse_department_routing_payload(
            {
                "departments": {
                    "p1ivd5m8": {
                        "mode": "team_pool",
                        "agent_keys": ["a1"],
                        "active": True,
                        "owner_overrides": {"some page": "b2"},
                    }
                }
            },
            source="test",
        )


def test_parse_department_routing_owner_override_rejects_admin_login_key() -> None:
    with pytest.raises(ValueError, match="admin login"):
        auto_assign._parse_department_routing_payload(
            {
                "departments": {
                    "p1ivd5m8": {
                        "mode": "team_pool",
                        "agent_keys": [auto_assign.ADMIN_LOGIN_AGENT_KEY],
                        "active": True,
                        "owner_overrides": {"some page": auto_assign.ADMIN_LOGIN_AGENT_KEY},
                    }
                }
            },
            source="test",
        )


# --- full_pool mode (must reproduce legacy behavior) -------------------------


def test_full_pool_mapped_department_matches_legacy_round_robin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2", "c3"],
        {"o6woli37": {"label": "AutoCenter", "mode": "full_pool", "agent_keys": [], "active": True}},
        monkeypatch,
    )
    now = _now_in_window()
    picks = [
        auto_assign.assign_round_robin(
            conv_code=f"c{idx}", incoming_agent_id=None, now=now, department_id="o6woli37"
        )["agent_id"]
        for idx in range(4)
    ]
    assert picks == ["a1", "b2", "c3", "a1"]


def test_full_pool_mapped_department_quota_cutoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["a1", "b2"],
        {"o6woli37": {"label": "AutoCenter", "mode": "full_pool", "agent_keys": [], "active": True}},
        monkeypatch,
    )
    monkeypatch.setattr(auto_assign, "MAX_ASSIGNMENTS_PER_AGENT", 2)
    now = _now_in_window()
    for idx in range(4):
        result = auto_assign.assign_round_robin(
            conv_code=f"c{idx}", incoming_agent_id=None, now=now, department_id="o6woli37"
        )
        assert result["status"] == "assigned"
    result = auto_assign.assign_round_robin(
        conv_code="c5", incoming_agent_id=None, now=now, department_id="o6woli37"
    )
    assert result["status"] == "no_eligible_agents"
    assert result["reason"] == "quota_reached"


# --- legacy / unmapped behavior ----------------------------------------------


def test_no_department_id_keeps_legacy_behavior(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1", "b2"], {}, monkeypatch)
    now = _now_in_window()
    picks = [
        auto_assign.assign_round_robin(conv_code=f"c{idx}", incoming_agent_id=None, now=now)["agent_id"]
        for idx in range(3)
    ]
    assert picks == ["a1", "b2", "a1"]


def test_unmapped_department_fails_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1", "b2"], {}, monkeypatch)
    result = auto_assign.plan_next_assignment(now=_now_in_window(), department_id="unknown-dept")
    assert result["status"] == "no_eligible_agents"
    assert result["reason"] == "unmapped_department"


def test_inactive_department_route_fails_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(
        tmp_path,
        ["a1"],
        {"mqemg9w7": {"label": "Angelo", "mode": "direct", "agent_keys": ["a1"], "active": False}},
        monkeypatch,
    )
    result = auto_assign.plan_next_assignment(now=_now_in_window(), department_id="mqemg9w7")
    assert result["status"] == "no_eligible_agents"
    assert result["reason"] == "unmapped_department"


# --- payload validation -------------------------------------------------------


def test_parse_department_routing_rejects_default_id() -> None:
    with pytest.raises(ValueError, match="reserved"):
        auto_assign._parse_department_routing_payload(
            {"departments": {"default": {"mode": "full_pool", "agent_keys": [], "active": True}}},
            source="test",
        )


def test_parse_department_routing_rejects_admin_login_key() -> None:
    with pytest.raises(ValueError, match="admin login"):
        auto_assign._parse_department_routing_payload(
            {
                "departments": {
                    "mqemg9w7": {
                        "mode": "direct",
                        "agent_keys": [auto_assign.ADMIN_LOGIN_AGENT_KEY],
                        "active": True,
                    }
                }
            },
            source="test",
        )


def test_parse_department_routing_direct_requires_single_agent_key() -> None:
    with pytest.raises(ValueError, match="exactly one agent_key"):
        auto_assign._parse_department_routing_payload(
            {
                "departments": {
                    "mqemg9w7": {
                        "mode": "direct",
                        "agent_keys": ["a1", "a2"],
                        "active": True,
                    }
                }
            },
            source="test",
        )
