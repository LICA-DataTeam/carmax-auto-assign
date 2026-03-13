from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.api.utils.logging import get_logger, log_event


MAX_ASSIGNMENTS_PER_AGENT = 30
TIMEZONE = "Asia/Manila"
WINDOW_START_HOUR = 8
WINDOW_START_MINUTE = 30
CONFIG_ENV = "AUTO_ASSIGN_CONFIG_PATH"
STATE_ENV = "AUTO_ASSIGN_STATE_PATH"

_STATE_LOCK = threading.Lock()
logger = get_logger(__name__)


@dataclass(frozen=True)
class Agent:
    agent_id: str
    agent_name: str
    active: bool


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _config_path() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "config" / "carmax_agents.json"


def _state_path() -> Path:
    override = os.getenv(STATE_ENV)
    if override:
        return Path(override)
    return _repo_root() / "data" / "auto_assign_state.json"


def _parse_active_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def load_agents() -> List[Agent]:
    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(f"Agent config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    teams = payload.get("teams") or []
    agents: List[Agent] = []
    for team in teams:
        for item in team.get("agents") or []:
            agent_id = str(item.get("agent_key") or "").strip()
            agent_name = str(item.get("agent_name") or "").strip()
            if not agent_id:
                continue
            agents.append(
                Agent(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    active=_parse_active_flag(item.get("active", True)),
                )
            )
    return agents


def is_within_window(now: Optional[datetime] = None) -> bool:
    tz = ZoneInfo(TIMEZONE)
    current = now.astimezone(tz) if now else datetime.now(tz)
    if current.hour < WINDOW_START_HOUR:
        return False
    if current.hour == WINDOW_START_HOUR and current.minute < WINDOW_START_MINUTE:
        return False
    return True


def _today_key(now: Optional[datetime] = None) -> str:
    tz = ZoneInfo(TIMEZONE)
    current = now.astimezone(tz) if now else datetime.now(tz)
    return current.strftime("%Y-%m-%d")


def _default_state() -> Dict[str, object]:
    return {
        "assignments": {},
        "daily": {},
    }


def _load_state() -> Dict[str, object]:
    path = _state_path()
    if not path.exists():
        return _default_state()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()


def _save_state(state: Dict[str, object]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")


def _ensure_daily_state(state: Dict[str, object], date_key: str, agent_ids: List[str]) -> Dict[str, object]:
    daily = state.setdefault("daily", {})
    day_state = daily.setdefault(date_key, {"last_index": -1, "counts": {}})
    counts = day_state.setdefault("counts", {})
    for agent_id in agent_ids:
        counts.setdefault(agent_id, 0)
    return day_state


def _select_round_robin(
    agents: List[Agent],
    counts: Dict[str, int],
    last_index: int,
) -> Optional[Tuple[int, Agent]]:
    if not agents:
        return None
    total = len(agents)
    start = (last_index + 1) % total if last_index >= 0 else 0
    for offset in range(total):
        idx = (start + offset) % total
        agent = agents[idx]
        if counts.get(agent.agent_id, 0) < MAX_ASSIGNMENTS_PER_AGENT:
            return idx, agent
    return None


def assign_round_robin(
    *,
    conv_code: str,
    incoming_agent_id: Optional[str],
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    with _STATE_LOCK:
        state = _load_state()
        assignments = state.setdefault("assignments", {})

        existing = assignments.get(conv_code)
        if existing:
            log_event(
                logger,
                "auto_assign_replay",
                conv_code=conv_code,
                agent_id=existing.get("agent_id"),
            )
            return {
                "status": "already_assigned",
                "conv_code": conv_code,
                "agent_id": existing.get("agent_id"),
                "reason": "idempotent_replay",
            }

        if incoming_agent_id:
            record = {
                "status": "already_assigned",
                "conv_code": conv_code,
                "agent_id": incoming_agent_id,
                "reason": "incoming_agent_id",
                "created_at": current.isoformat(),
            }
            assignments[conv_code] = record
            _save_state(state)
            log_event(
                logger,
                "auto_assign_incoming_agent",
                conv_code=conv_code,
                agent_id=incoming_agent_id,
            )
            return {
                "status": "already_assigned",
                "conv_code": conv_code,
                "agent_id": incoming_agent_id,
                "reason": "incoming_agent_id",
            }

        if not is_within_window(current):
            log_event(
                logger,
                "auto_assign_outside_hours",
                conv_code=conv_code,
            )
            return {
                "status": "outside_hours",
                "conv_code": conv_code,
                "agent_id": None,
                "reason": "outside_time_window",
            }

        agents = [agent for agent in load_agents() if agent.active]
        if not agents:
            return {
                "status": "no_eligible_agents",
                "conv_code": conv_code,
                "agent_id": None,
                "reason": "no_active_agents",
            }

        day_key = _today_key(current)
        day_state = _ensure_daily_state(state, day_key, [a.agent_id for a in agents])
        counts = day_state.get("counts", {})
        last_index = int(day_state.get("last_index", -1))

        pick = _select_round_robin(agents, counts, last_index)
        if pick is None:
            log_event(
                logger,
                "auto_assign_quota_reached",
                conv_code=conv_code,
            )
            return {
                "status": "no_eligible_agents",
                "conv_code": conv_code,
                "agent_id": None,
                "reason": "quota_reached",
            }

        next_index, chosen = pick
        counts[chosen.agent_id] = int(counts.get(chosen.agent_id, 0)) + 1
        day_state["last_index"] = next_index
        day_state["last_agent_id"] = chosen.agent_id

        record = {
            "status": "assigned",
            "conv_code": conv_code,
            "agent_id": chosen.agent_id,
            "reason": "round_robin",
            "created_at": current.isoformat(),
        }
        assignments[conv_code] = record
        _save_state(state)
        log_event(
            logger,
            "auto_assign_success",
            conv_code=conv_code,
            agent_id=chosen.agent_id,
        )

        return {
            "status": "assigned",
            "conv_code": conv_code,
            "agent_id": chosen.agent_id,
            "reason": "round_robin",
        }
