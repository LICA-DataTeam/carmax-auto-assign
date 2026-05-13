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


def _write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _setup_env(tmp_path: Path, agent_ids: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "agents.json"
    state_path = tmp_path / "state.json"
    _write_agents(config_path, agent_ids)
    monkeypatch.setenv("AUTO_ASSIGN_STORE", "file")
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "file")
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(state_path))
    reset_store_cache()


def _now_in_window() -> datetime:
    return datetime(2026, 3, 13, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))


def _now_outside_window() -> datetime:
    return datetime(2026, 3, 13, 8, 0, tzinfo=ZoneInfo("Asia/Manila"))


def _now_after_window() -> datetime:
    return datetime(2026, 3, 13, 18, 0, tzinfo=ZoneInfo("Asia/Manila"))


def test_idempotency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1", "b2"], monkeypatch)
    first = auto_assign.assign_round_robin(conv_code="c1", incoming_agent_id=None, now=_now_in_window())
    second = auto_assign.assign_round_robin(conv_code="c1", incoming_agent_id=None, now=_now_in_window())
    assert first["status"] == "assigned"
    assert second["status"] == "already_assigned"
    assert first["agent_id"] == second["agent_id"]


def test_round_robin_wraparound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1", "b2", "c3"], monkeypatch)
    now = _now_in_window()
    picks = [
        auto_assign.assign_round_robin(conv_code="c1", incoming_agent_id=None, now=now)["agent_id"],
        auto_assign.assign_round_robin(conv_code="c2", incoming_agent_id=None, now=now)["agent_id"],
        auto_assign.assign_round_robin(conv_code="c3", incoming_agent_id=None, now=now)["agent_id"],
        auto_assign.assign_round_robin(conv_code="c4", incoming_agent_id=None, now=now)["agent_id"],
    ]
    assert picks == ["a1", "b2", "c3", "a1"]


def test_quota_cutoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1", "b2"], monkeypatch)
    monkeypatch.setattr(auto_assign, "MAX_ASSIGNMENTS_PER_AGENT", 2)
    now = _now_in_window()
    for idx in range(4):
        result = auto_assign.assign_round_robin(conv_code=f"c{idx}", incoming_agent_id=None, now=now)
        assert result["status"] == "assigned"
    result = auto_assign.assign_round_robin(conv_code="c5", incoming_agent_id=None, now=now)
    assert result["status"] == "no_eligible_agents"
    assert result["reason"] == "quota_reached"


def test_outside_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1"], monkeypatch)
    result = auto_assign.assign_round_robin(conv_code="c1", incoming_agent_id=None, now=_now_outside_window())
    assert result["status"] == "outside_hours"


def test_outside_window_after_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_env(tmp_path, ["a1"], monkeypatch)
    result = auto_assign.assign_round_robin(conv_code="c1", incoming_agent_id=None, now=_now_after_window())
    assert result["status"] == "outside_hours"


def test_load_agents_duplicate_agent_key_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    payload = {
        "teams": [
            {
                "team": "A",
                "agents": [
                    {"agent_key": "a1", "agent_name": "Agent A1", "active": "true"},
                    {"agent_key": "a1", "agent_name": "Agent A1 Duplicate", "active": "true"},
                ],
            }
        ]
    }
    _write_payload(config_path, payload)
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.delenv(auto_assign.AGENTS_SOURCE_ENV, raising=False)

    with pytest.raises(ValueError, match="Duplicate agent_key"):
        auto_assign.load_agents()


def test_load_agents_missing_agent_key_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    payload = {
        "teams": [
            {
                "team": "A",
                "agents": [
                    {"agent_name": "Agent Missing Key", "active": "true"},
                ],
            }
        ]
    }
    _write_payload(config_path, payload)
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.delenv(auto_assign.AGENTS_SOURCE_ENV, raising=False)

    with pytest.raises(ValueError, match="Missing required 'agent_key'"):
        auto_assign.load_agents()


def test_load_agents_invalid_teams_type_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    payload = {"teams": {"team": "A", "agents": []}}
    _write_payload(config_path, payload)
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.delenv(auto_assign.AGENTS_SOURCE_ENV, raising=False)

    with pytest.raises(ValueError, match="Agent config 'teams' must be a list"):
        auto_assign.load_agents()


def test_load_agents_firestore_source_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "teams": [
            {
                "team": "X",
                "agents": [
                    {"agent_key": "x1", "agent_name": "Agent X1", "active": "yes"},
                    {"agent_key": "x2", "agent_name": "Agent X2", "active": "0"},
                ],
            }
        ]
    }
    monkeypatch.setenv(auto_assign.AGENTS_SOURCE_ENV, "firestore")
    monkeypatch.setattr(auto_assign, "_load_agents_payload_from_firestore", lambda: payload)

    agents = auto_assign.load_agents()
    assert [agent.agent_id for agent in agents] == ["x1", "x2"]
    assert [agent.active for agent in agents] == [True, False]
