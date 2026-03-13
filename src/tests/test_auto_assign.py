import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.api.services import auto_assign


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


def _setup_env(tmp_path: Path, agent_ids: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "agents.json"
    state_path = tmp_path / "state.json"
    _write_agents(config_path, agent_ids)
    monkeypatch.setenv(auto_assign.CONFIG_ENV, str(config_path))
    monkeypatch.setenv(auto_assign.STATE_ENV, str(state_path))


def _now_in_window() -> datetime:
    return datetime(2026, 3, 13, 9, 0, tzinfo=ZoneInfo("Asia/Manila"))


def _now_outside_window() -> datetime:
    return datetime(2026, 3, 13, 8, 0, tzinfo=ZoneInfo("Asia/Manila"))


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
