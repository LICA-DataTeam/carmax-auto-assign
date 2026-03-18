from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.api.utils.logging import get_logger, log_event
from src.api.services.auto_assign_store import get_store


MAX_ASSIGNMENTS_PER_AGENT = 30
TIMEZONE = "Asia/Manila"
WINDOW_START_HOUR = 8
WINDOW_START_MINUTE = 30
CONFIG_ENV = "AUTO_ASSIGN_CONFIG_PATH"
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


def get_existing_assignment(conv_code: str) -> Optional[Dict[str, object]]:
    store = get_store()
    return store.get_assignment(conv_code)


def record_existing_assignment(
    *,
    conv_code: str,
    agent_id: str,
    reason: str,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    store = get_store()
    return store.record_assignment(
        conv_code=conv_code,
        agent_id=agent_id,
        reason=reason,
        status="already_assigned",
        now=current,
    )


def plan_next_assignment(now: Optional[datetime] = None) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    if not is_within_window(current):
        return {
            "status": "outside_hours",
            "agent_id": None,
            "reason": "outside_time_window",
        }

    agents = [agent for agent in load_agents() if agent.active]
    if not agents:
        return {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "no_active_agents",
        }

    store = get_store()
    day_key = _today_key(current)
    day_state = store.get_daily_state(day_key)
    counts = dict(day_state.get("counts") or {})
    last_index = int(day_state.get("last_index", -1))

    pick = _select_round_robin(agents, counts, last_index)
    if pick is None:
        return {
            "status": "no_eligible_agents",
            "agent_id": None,
            "reason": "quota_reached",
        }

    next_index, chosen = pick
    return {
        "status": "candidate",
        "agent_id": chosen.agent_id,
        "reason": "round_robin",
        "day_key": day_key,
        "next_index": next_index,
    }

def commit_assignment(
    *,
    conv_code: str,
    agent_id: str,
    reason: str,
    now: Optional[datetime] = None,
    day_key: Optional[str] = None,
    next_index: Optional[int] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    store = get_store()
    if day_key is None:
        day_key = _today_key(current)
    return store.commit_assignment(
        conv_code=conv_code,
        agent_id=agent_id,
        reason=reason,
        date_key=day_key,
        next_index=next_index,
        max_assignments=MAX_ASSIGNMENTS_PER_AGENT,
        now=current,
    )


def assign_round_robin(
    *,
    conv_code: str,
    incoming_agent_id: Optional[str],
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    current = now.astimezone(ZoneInfo(TIMEZONE)) if now else datetime.now(ZoneInfo(TIMEZONE))
    existing = get_existing_assignment(conv_code)
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
        record = record_existing_assignment(
            conv_code=conv_code,
            agent_id=incoming_agent_id,
            reason="incoming_agent_id",
            now=current,
        )
        log_event(
            logger,
            "auto_assign_incoming_agent",
            conv_code=conv_code,
            agent_id=incoming_agent_id,
        )
        return {
            "status": "already_assigned",
            "conv_code": conv_code,
            "agent_id": record.get("agent_id"),
            "reason": "incoming_agent_id",
        }

    plan = plan_next_assignment(now=current)
    if plan.get("status") != "candidate":
        status = plan.get("status")
        reason = plan.get("reason")
        if status == "outside_hours":
            log_event(logger, "auto_assign_outside_hours", conv_code=conv_code)
        if status == "no_eligible_agents" and reason == "quota_reached":
            log_event(logger, "auto_assign_quota_reached", conv_code=conv_code)
        return {
            "status": status,
            "conv_code": conv_code,
            "agent_id": plan.get("agent_id"),
            "reason": reason,
        }

    commit = commit_assignment(
        conv_code=conv_code,
        agent_id=str(plan.get("agent_id")),
        reason=str(plan.get("reason") or "round_robin"),
        now=current,
        day_key=str(plan.get("day_key")),
        next_index=plan.get("next_index"),
    )
    if commit.get("status") == "assigned":
        log_event(
            logger,
            "auto_assign_success",
            conv_code=conv_code,
            agent_id=commit.get("agent_id"),
        )
    return commit
