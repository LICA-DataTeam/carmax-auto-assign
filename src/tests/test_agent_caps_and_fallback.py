"""Per-agent daily caps (from each agent's configured `max`), the shared daily
total across pools, the full_pool fallback for departments whose own agents
are unavailable, and the Firestore daily-state write shape."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

from src.api.services import auto_assign
from src.api.services.auto_assign_store import FULL_POOL_KEY, _daily_updates, get_store, reset_store_cache


NOW = datetime(2026, 11, 27, 10, 0, tzinfo=ZoneInfo("Asia/Manila"))

MONIQUE, CHESKA, RAF, LUKE = "iq74g6mz", "ba6qhkwv", "57qw5x5n", "5xueee4s"


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agents: list[dict],
    departments: Optional[dict] = None,
) -> None:
    config_path = tmp_path / "agents.json"
    routing_path = tmp_path / "routing.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "teams": [
                    {
                        "team": "A",
                        "agents": [
                            {"agent_name": f"Agent {a['agent_key']}", "active": "true", **a}
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
    monkeypatch.setenv("AUTO_ASSIGN_STATE_PATH", str(state_path))
    reset_store_cache()


def _assign(conv_code: str, department_id: Optional[str] = None) -> dict:
    return auto_assign.assign_round_robin(
        conv_code=conv_code, incoming_agent_id=None, now=NOW, department_id=department_id
    )


def _counts() -> dict:
    return get_store().get_daily_state("2026-11-27")["counts"]


# --- parsing the configured max ---------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [(12, 12), ("15", 15), (0, 0), (None, 30), ("", 30), ("abc", 30), (-5, 30), (True, 30)],
)
def test_parse_max_assignments(raw, expected) -> None:
    assert auto_assign._parse_max_assignments(raw) == expected


def test_load_agents_reads_each_agents_max(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [
            {"agent_key": MONIQUE, "max": 15},
            {"agent_key": "x1"},  # no max configured -> fallback
        ],
    )
    by_id = {a.agent_id: a for a in auto_assign.load_agents()}
    assert by_id[MONIQUE].max_assignments == 15
    assert by_id["x1"].max_assignments == auto_assign.MAX_ASSIGNMENTS_PER_AGENT == 30


# --- per-agent cap in round-robin --------------------------------------------


def test_capped_agent_is_skipped_once_at_their_own_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": MONIQUE, "max": 2}, {"agent_key": "x1", "max": 30}],
    )
    picks = [_assign(f"c{i}")["agent_id"] for i in range(6)]
    assert picks.count(MONIQUE) == 2
    assert picks == [MONIQUE, "x1", MONIQUE, "x1", "x1", "x1"]
    assert _counts() == {MONIQUE: 2, "x1": 4}


def test_agent_with_max_zero_gets_no_round_robin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(tmp_path, monkeypatch, [{"agent_key": LUKE, "max": 0}, {"agent_key": "x1"}])
    picks = [_assign(f"c{i}")["agent_id"] for i in range(3)]
    assert LUKE not in picks


def test_cap_is_one_total_across_pools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": RAF, "max": 2}, {"agent_key": "x1", "max": 30}],
        departments={
            "q1h4yk8b": {"mode": "team_pool", "agent_keys": [RAF, "x1"], "active": True},
        },
    )
    assert _assign("t1", "q1h4yk8b")["agent_id"] == RAF  # team pool
    assert _assign("f1")["agent_id"] == RAF  # full pool - same daily counter
    # Raf is now at 2 total; neither pool may pick him again today.
    assert _assign("t2", "q1h4yk8b")["agent_id"] == "x1"
    assert _assign("f2")["agent_id"] == "x1"
    assert _assign("f3")["agent_id"] == "x1"
    assert _counts()[RAF] == 2


def test_direct_counts_toward_total_but_is_not_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": CHESKA, "max": 2}, {"agent_key": "x1", "max": 30}],
        departments={"cheska-page": {"mode": "direct", "agent_keys": [CHESKA], "active": True}},
    )
    # Her own page always reaches her, even past her cap...
    for i in range(3):
        result = _assign(f"d{i}", "cheska-page")
        assert result["status"] == "assigned"
        assert result["agent_id"] == CHESKA
    assert _counts()[CHESKA] == 3
    # ...but those leads count, so round-robin no longer feeds her today.
    assert {_assign(f"f{i}")["agent_id"] for i in range(3)} == {"x1"}


def test_owner_override_counts_toward_total(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        [{"agent_key": MONIQUE, "max": 1}, {"agent_key": "x1", "max": 30}],
        departments={
            "p1": {
                "mode": "team_pool",
                "agent_keys": [MONIQUE, "x1"],
                "active": True,
                "owner_overrides": {"monique page": MONIQUE},
            }
        },
    )
    plan = auto_assign.plan_next_assignment(now=NOW, department_id="p1", owner_name="Monique Page")
    assert plan["reason"] == "team_pool_owner_override"
    commit = auto_assign.commit_assignment(
        conv_code="o1",
        agent_id=plan["agent_id"],
        reason=plan["reason"],
        now=NOW,
        day_key=plan["day_key"],
        next_index=plan["next_index"],
        pool_key=plan["pool_key"],
        bypass_quota=plan["bypass_quota"],
        max_assignments=plan["max_assignments"],
    )
    assert commit["status"] == "assigned"
    assert _counts()[MONIQUE] == 1
    assert _assign("f1")["agent_id"] == "x1"


def test_commit_enforces_cap_even_if_plan_was_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, [{"agent_key": MONIQUE, "max": 1}])
    plan_a = auto_assign.plan_next_assignment(now=NOW)
    plan_b = auto_assign.plan_next_assignment(now=NOW)  # concurrent request, same view
    kwargs = dict(now=NOW, reason="round_robin", pool_key=FULL_POOL_KEY, bypass_quota=False)
    first = auto_assign.commit_assignment(
        conv_code="a", agent_id=MONIQUE, day_key=plan_a["day_key"],
        next_index=plan_a["next_index"], max_assignments=plan_a["max_assignments"], **kwargs
    )
    second = auto_assign.commit_assignment(
        conv_code="b", agent_id=MONIQUE, day_key=plan_b["day_key"],
        next_index=plan_b["next_index"], max_assignments=plan_b["max_assignments"], **kwargs
    )
    assert first["status"] == "assigned"
    assert second["status"] == "no_eligible_agents"
    assert second["reason"] == "quota_reached"


# --- fallback when a department's own agents are unavailable ----------------


SE_PAGES = {
    "2vhsyd6w": {"label": "Shaira & Win", "mode": "team_pool", "agent_keys": ["shaira", "win"], "active": True},
    "mqemg9w7": {"label": "Angelo & Luke", "mode": "direct", "agent_keys": ["angelo"], "active": True},
}
ROSTER = [{"agent_key": k} for k in ["shaira", "win", "angelo", "o1", "o2"]]


def test_team_pool_all_offline_falls_back_to_full_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, ROSTER, SE_PAGES)
    offline = {"shaira", "win", "o1"}
    plan = auto_assign.plan_next_assignment(now=NOW, department_id="2vhsyd6w", exclude_agent_ids=offline)
    assert plan["status"] == "candidate"
    assert plan["reason"] == "team_fallback_full_pool"
    assert plan["agent_id"] not in offline
    assert plan["pool_key"] == FULL_POOL_KEY
    assert plan["fallback_from"] == "2vhsyd6w"
    assert plan["team_cause"] == "no_available_team_agents"


def test_direct_agent_offline_falls_back_to_full_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, ROSTER, SE_PAGES)
    plan = auto_assign.plan_next_assignment(now=NOW, department_id="mqemg9w7", exclude_agent_ids={"angelo"})
    assert plan["status"] == "candidate"
    assert plan["reason"] == "team_fallback_full_pool"
    assert plan["agent_id"] != "angelo"
    assert plan["bypass_quota"] is False


def test_fallback_assignment_is_committed_and_rotates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, ROSTER, SE_PAGES)
    offline = {"shaira", "win"}
    picks = []
    for i in range(3):
        plan = auto_assign.plan_next_assignment(now=NOW, department_id="2vhsyd6w", exclude_agent_ids=offline)
        commit = auto_assign.commit_assignment(
            conv_code=f"c{i}", agent_id=plan["agent_id"], reason=plan["reason"], now=NOW,
            day_key=plan["day_key"], next_index=plan["next_index"], pool_key=plan["pool_key"],
            bypass_quota=plan["bypass_quota"], max_assignments=plan["max_assignments"],
        )
        assert commit["status"] == "assigned"
        assert commit["reason"] == "team_fallback_full_pool"
        picks.append(commit["agent_id"])
    assert picks == ["angelo", "o1", "o2"]


def test_team_and_full_pool_all_offline_reports_no_available_team_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, ROSTER, SE_PAGES)
    everyone = {a["agent_key"] for a in ROSTER}
    plan = auto_assign.plan_next_assignment(now=NOW, department_id="2vhsyd6w", exclude_agent_ids=everyone)
    assert plan["status"] == "no_eligible_agents"
    assert plan["reason"] == "no_available_team_agents"
    assert plan["reason"] != "quota_reached"


def test_full_pool_all_offline_reports_no_available_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, ROSTER)
    everyone = {a["agent_key"] for a in ROSTER}
    plan = auto_assign.plan_next_assignment(now=NOW, exclude_agent_ids=everyone)
    assert plan["status"] == "no_eligible_agents"
    assert plan["reason"] == "no_available_agents"


def test_team_pool_with_one_online_member_stays_in_team(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(tmp_path, monkeypatch, ROSTER, SE_PAGES)
    plan = auto_assign.plan_next_assignment(now=NOW, department_id="2vhsyd6w", exclude_agent_ids={"shaira"})
    assert plan["reason"] == "team_pool"
    assert plan["agent_id"] == "win"


# --- Firestore daily-state write shape --------------------------------------


class _FakeFirestore:
    class Increment:
        def __init__(self, value: int) -> None:
            self.value = value


def test_daily_updates_use_nested_maps_not_dotted_keys() -> None:
    updates = _daily_updates(
        _FakeFirestore, date_key="2026-11-27", agent_id=MONIQUE, next_index=3,
        pool_key="team:q1h4yk8b", now=NOW,
    )
    assert not any("." in key for key in updates)
    assert isinstance(updates["counts"][MONIQUE], _FakeFirestore.Increment)
    assert updates["pools"] == {"team:q1h4yk8b": {"last_index": 3, "last_agent_id": MONIQUE}}


def test_daily_updates_full_pool_cursor_and_always_counts() -> None:
    updates = _daily_updates(
        _FakeFirestore, date_key="2026-11-27", agent_id="x1", next_index=None,
        pool_key="direct:dept", now=NOW,
    )
    assert "counts" in updates  # direct/owner_override still count toward the total
    assert "pools" not in updates and "last_index" not in updates

    full = _daily_updates(
        _FakeFirestore, date_key="2026-11-27", agent_id="x1", next_index=0,
        pool_key=FULL_POOL_KEY, now=NOW,
    )
    assert full["last_index"] == 0


def test_daily_updates_resolve_to_nested_firestore_field_paths() -> None:
    """Guards the production bug: with set(merge=True), the client library must
    see ('counts', <id>) - not a single literal 'counts.<id>' field."""
    firestore = pytest.importorskip("google.cloud.firestore")
    from google.cloud.firestore_v1._helpers import DocumentExtractorForMerge
    from google.cloud.firestore_v1.field_path import FieldPath

    updates = _daily_updates(
        firestore, date_key="2026-11-27", agent_id=MONIQUE, next_index=1,
        pool_key="team:q1h4yk8b", now=NOW,
    )
    extractor = DocumentExtractorForMerge(updates)
    assert FieldPath("counts", MONIQUE) in extractor.transform_paths
    assert extractor.set_fields["pools"]["team:q1h4yk8b"]["last_index"] == 1
    assert not any("." in key for key in extractor.set_fields)
